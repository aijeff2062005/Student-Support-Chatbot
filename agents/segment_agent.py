import json
import logging
import re
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.llm_agent import LlmAgent
from google.adk.models import LlmRequest, LlmResponse
from google.adk.models.lite_llm import LiteLlm
from google.genai import types
from pydantic import ValidationError

from callbacks.advisor_response_agent import check_if_agent_should_run
from configs.llm_client import get_litellm_config
from prompts.segment_agent import SYSTEM_PROMPT
from schemas.segment_agent import SegmentAgentOutput
from tools.data_collection_tools import _call_segment_api

logger = logging.getLogger(__name__)

_SEGMENT_FIELDS = tuple(
    field_name
    for field_name in SegmentAgentOutput.model_fields
    if field_name.endswith("_keywords")
)
_SEGMENT_REASONING_FIELD = "reasoning_explain"
_SEGMENT_ORIGINAL_MESSAGE_FIELD = "original_message"
_EXPLANATION_MARKERS = (
    "tuy nhiên",
    "vậy nên",
    "vậy kết quả",
    "kết quả sẽ là",
    "theo quy tắc",
    "theo luật",
    "trong trường hợp này",
    "nếu xét",
    "có thể là tín hiệu",
    "keyword hợp lệ",
    "giải thích",
    "phân loại đối tượng",
)
_GENERIC_SEGMENT_PHRASES = {
    "ngành",
    "trường",
    "khoa",
    "các",
    "các ngành",
    "ngành nào",
    "trường nào",
    "như nào",
    "như thế nào",
    "là gì",
    "bao nhiêu",
    "có không",
    "ra sao",
    "ở đâu",
    "khi nào",
    "trường có những ngành nào",
    "học phí các ngành ở trường như nào",
}
_GENERIC_SEGMENT_TOKENS = {
    "bao",
    "biết",
    "các",
    "có",
    "gì",
    "gồm",
    "khoa",
    "không",
    "là",
    "nào",
    "ngành",
    "nhiêu",
    "những",
    "như",
    "ở",
    "ra",
    "sao",
    "tại",
    "thế",
    "trường",
}


def _extract_text_from_event(event: Any) -> str | None:
    content = getattr(event, "content", None)
    parts = getattr(content, "parts", None)
    if not isinstance(parts, list) or not parts:
        return None

    text = getattr(parts[0], "text", None)
    if not isinstance(text, str) or not text.strip():
        return None

    return text.strip()


def _get_latest_user_message(callback_context: CallbackContext) -> str | None:
    for event in reversed(getattr(callback_context.session, "events", []) or []):
        event_author = getattr(event, "author", getattr(event, "role", "unknown"))
        if event_author != "user":
            continue

        text = _extract_text_from_event(event)
        if text:
            return text

    return None


def _get_current_user_message(callback_context: CallbackContext) -> str | None:
    for key in (
        "current_user_raw_message",
        "current_user_message",
        "original_user_message",
        "final_rewritten_user_message",
    ):
        value = callback_context.state.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    return _get_latest_user_message(callback_context)


def _strip_markdown_fences(content: str) -> str:
    if "```json" in content:
        content = content.replace("```json", "").replace("```", "")
    elif "```" in content:
        content = content.replace("```", "")
    return content.strip()


def _clean_keyword_text(value: Any) -> str:
    if value is None:
        return ""

    text = str(value).strip()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r'^[\s"\'“”‘’.,!?;:()\[\]{}]+|[\s"\'“”‘’.,!?;:()\[\]{}]+$', "", text)
    return text


def _clean_reasoning_text(value: Any) -> str:
    if value is None:
        return ""

    text = str(value).strip()
    text = re.sub(r"\s+", " ", text)
    return text[:500]


def _clean_original_message_text(value: Any) -> str:
    if value is None:
        return ""

    text = str(value).strip()
    text = re.sub(r"\s+", " ", text)
    return text[:500]


def _keyword_match_key(keyword: str) -> str:
    return " ".join(keyword.lower().split())


def _get_rewrite_original_message(callback_context: CallbackContext) -> str | None:
    rewrite_results = callback_context.state.get("normalized_message_rewrite_result")
    if isinstance(rewrite_results, list):
        original_messages: list[str] = []
        seen_original_messages: set[str] = set()
        for result in rewrite_results:
            if not isinstance(result, dict):
                continue
            original_message = result.get("original_message")
            if isinstance(original_message, str) and original_message.strip():
                cleaned_message = original_message.strip()
                if cleaned_message not in seen_original_messages:
                    seen_original_messages.add(cleaned_message)
                    original_messages.append(cleaned_message)

        if original_messages:
            return "\n".join(original_messages)

    for key in (
        "original_user_message",
        "current_user_raw_message",
        "message_rewrite_latest_user",
    ):
        value = callback_context.state.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    return None


def _looks_like_explanation(keyword: str) -> bool:
    keyword_key = _keyword_match_key(keyword)
    if any(marker in keyword_key for marker in _EXPLANATION_MARKERS):
        return True

    if any(symbol in keyword for symbol in (":", "[", "]", "{", "}")):
        return True

    return False


def _is_generic_segment_keyword(keyword: str) -> bool:
    if not keyword:
        return True

    if _looks_like_explanation(keyword):
        return True

    keyword_key = _keyword_match_key(keyword)
    if keyword_key in _GENERIC_SEGMENT_PHRASES:
        return True

    tokens = keyword.split()
    if not tokens:
        return True

    token_keys = [_keyword_match_key(token) for token in tokens]

    if all(token in _GENERIC_SEGMENT_TOKENS for token in token_keys):
        return True

    return False


def _prune_redundant_group_keywords(keywords: list[str]) -> list[str]:
    pruned: list[str] = []
    keyword_keys = [_keyword_match_key(keyword) for keyword in keywords]

    for keyword in keywords:
        keyword_key = _keyword_match_key(keyword)
        has_longer_overlap = any(
            keyword_key in existing_key and len(existing_key) > len(keyword_key)
            for existing_key in keyword_keys
            if existing_key != keyword_key
        )
        if has_longer_overlap:
            continue

        pruned.append(keyword)

    return pruned


def _sanitize_segment_output(payload: SegmentAgentOutput) -> SegmentAgentOutput:
    sanitized: dict[str, Any] = {}
    seen_keywords: set[str] = set()

    for field_name in _SEGMENT_FIELDS:
        raw_values = getattr(payload, field_name, [])
        cleaned_values: list[str] = []

        for raw_value in raw_values:
            keyword = _clean_keyword_text(raw_value)
            if not keyword or _is_generic_segment_keyword(keyword):
                continue
            keyword_key = _keyword_match_key(keyword)
            if keyword_key in seen_keywords:
                continue

            seen_keywords.add(keyword_key)
            cleaned_values.append(keyword)

        sanitized[field_name] = _prune_redundant_group_keywords(cleaned_values)

    sanitized[_SEGMENT_REASONING_FIELD] = _clean_reasoning_text(
        getattr(payload, _SEGMENT_REASONING_FIELD, "")
    )
    sanitized[_SEGMENT_ORIGINAL_MESSAGE_FIELD] = _clean_original_message_text(
        getattr(payload, _SEGMENT_ORIGINAL_MESSAGE_FIELD, "")
    )
    return SegmentAgentOutput.model_validate(sanitized)


def _get_existing_segment_features(callback_context: CallbackContext) -> SegmentAgentOutput:
    existing_features = callback_context.state.get("customer_features", {})
    if not isinstance(existing_features, dict):
        return SegmentAgentOutput()

    try:
        parsed_existing = SegmentAgentOutput.model_validate(existing_features)
        return _sanitize_segment_output(parsed_existing)
    except ValidationError:
        logger.exception("[SegmentAgent] Failed to parse existing customer_features; resetting to empty payload")
        return SegmentAgentOutput()


def _merge_segment_features(existing: SegmentAgentOutput, current: SegmentAgentOutput) -> SegmentAgentOutput:
    merged_payload = {
        field_name: list(getattr(existing, field_name, [])) + list(getattr(current, field_name, []))
        for field_name in _SEGMENT_FIELDS
    }
    return _sanitize_segment_output(SegmentAgentOutput.model_validate(merged_payload))


def _normalize_segment_payload(raw_content: str | None) -> SegmentAgentOutput:
    if not raw_content:
        logger.warning("[SegmentAgent] Empty model payload; using empty segment features")
        return SegmentAgentOutput()

    try:
        payload = json.loads(_strip_markdown_fences(raw_content))
        if not isinstance(payload, dict):
            logger.info(
                "[SegmentAgent] Unexpected payload type %s; using empty segment features", type(payload).__name__
            )
            return SegmentAgentOutput()
        parsed_payload = SegmentAgentOutput.model_validate(payload)
        return _sanitize_segment_output(parsed_payload)
    except (json.JSONDecodeError, ValidationError) as exc:
        logger.error("[SegmentAgent] Failed to parse segment payload: %s; using empty segment features", exc)
        return SegmentAgentOutput()


def segment_agent_before_model_callback(
    callback_context: CallbackContext,
    llm_request: LlmRequest,
) -> LlmResponse | None:
    current_user_message = _get_current_user_message(callback_context)
    if not current_user_message:
        logger.warning("[SegmentAgent] Missing current user message before model call; keeping original request")
        return None

    original_message = _get_rewrite_original_message(callback_context) or current_user_message
    runtime_input = (
        "Runtime input for segmentation keyword extraction:\n"
        f"ORIGINAL_MESSAGE_FROM_REWRITE_AGENT:\n{original_message}\n\n"
        f"CURRENT_USER_MESSAGE:\n{current_user_message}\n\n"
        "Extract keywords only from CURRENT_USER_MESSAGE. "
        "Return original_message exactly equal to the message_rewrite_agent original_message when available."
    )

    llm_request.contents = [
        types.Content(
            role="user",
            parts=[types.Part(text=runtime_input)],
        )
    ]
    logger.info("[SegmentAgent] Injected current user message before model call")
    return None


def segment_agent_after_model_callback(
    callback_context: CallbackContext, llm_response: LlmResponse
) -> LlmResponse | None:
    if llm_response is not None and getattr(llm_response, "partial", None) not in (False, None):
        logger.warning("[SegmentAgent] Skipping partial LLM response")
        return None

    raw_content = None
    response_content = getattr(llm_response, "content", None)
    response_parts = getattr(response_content, "parts", None)
    if isinstance(response_parts, list) and response_parts:
        first_part = response_parts[0]
        raw_content = getattr(first_part, "text", None)

    current_payload = _normalize_segment_payload(raw_content)
    original_message = _get_rewrite_original_message(callback_context) or getattr(
        current_payload,
        _SEGMENT_ORIGINAL_MESSAGE_FIELD,
        "",
    )
    existing_payload = _get_existing_segment_features(callback_context)
    merged_payload = _merge_segment_features(existing_payload, current_payload)
    current_turn_features = {
        field_name: getattr(current_payload, field_name, [])
        for field_name in _SEGMENT_FIELDS
    }
    merged_features = {
        field_name: getattr(merged_payload, field_name, [])
        for field_name in _SEGMENT_FIELDS
    }
    segment_output = {
        _SEGMENT_REASONING_FIELD: getattr(current_payload, _SEGMENT_REASONING_FIELD, ""),
        _SEGMENT_ORIGINAL_MESSAGE_FIELD: original_message,
        **merged_features,
    }
    callback_context.state["current_turn_features_before_merge"] = current_turn_features
    callback_context.state["customer_features_after_merge"] = merged_features
    callback_context.state["customer_features"] = merged_features
    callback_context.state["segment_agent_output"] = segment_output

    if raw_content is not None:
        try:
            if isinstance(response_parts, list) and response_parts:
                response_parts[0].text = json.dumps(segment_output, ensure_ascii=False)
        except Exception:
            logger.exception("[SegmentAgent] Failed to overwrite raw segment output with sanitized payload")

    segment_result = _call_segment_api(features=merged_features, tool_context=callback_context)
    if not segment_result:
        segment_result = "Unknown"

    callback_context.state["latest_segments"] = segment_result
    callback_context.state["segment"] = segment_result
    logger.info("[SegmentAgent] Stored segment result: %s", segment_result)

    return llm_response


_cfg = get_litellm_config(task="classification_strict")
_cfg["max_tokens"] = 512

segment_agent = LlmAgent(
    model=LiteLlm(**_cfg),
    name="segment_agent",
    instruction=SYSTEM_PROMPT,
    before_agent_callback=check_if_agent_should_run,
    before_model_callback=segment_agent_before_model_callback,
    after_model_callback=segment_agent_after_model_callback,
    include_contents="none",
    output_schema=SegmentAgentOutput,
    output_key="segment_agent_output",
)
