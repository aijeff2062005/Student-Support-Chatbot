"""Callbacks for Message Rewrite Agent.

Handles overwriting the user message in the session history if a rewrite occurs.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.models import LlmRequest, LlmResponse
from google.genai import types

from agents.data_collector_agent import get_conversation_turns, get_fast_history
from callbacks.parent_intent_analyzer import check_admission_topic_skip
from services.qr_support import QR_ATTACHMENT_NOOP_STATE_KEY, QR_ERROR_FAST_PATH_STATE_KEY
from utils.json_utils import extract_json as _extract_json

logger = logging.getLogger(__name__)

SKIP_REWRITE_CATEGORIES = {1, 2}
REWRITE_CATEGORIES = {3, 4, 5, 6}
VALID_CATEGORIES = SKIP_REWRITE_CATEGORIES | REWRITE_CATEGORIES
CURRENT_USER_MESSAGE_KEY = "current_user_message"
CURRENT_USER_RAW_MESSAGE_KEY = "current_user_raw_message"
LAST_USER_RAW_MESSAGE_KEY = "last_user_raw_message"
LAST_USER_EFFECTIVE_MESSAGE_KEY = "last_user_effective_message"
LATEST_COMPLETED_AGENT_TURN_KEY = "latest_completed_agent_turn"
LATEST_COMPLETED_ANSWER_TURN_KEY = "latest_completed_answer_turn"
PREVIOUS_ANSWER_QUERY_RESPONSE_KEY = "previous_answer_query_response"
LATEST_PREVIOUS_TURN_KEY = "message_rewrite_latest_previous_turn"
LATEST_PREVIOUS_ANSWER_QUERY_RESPONSE_KEY = "message_rewrite_latest_previous_answer_query_response"
LATEST_ANSWER_FOLLOW_UP_QUESTIONS_KEY = "latest_answer_follow_up_questions"
RECENT_ANSWERED_MAJOR_NAMES_KEY = "recent_answered_major_names"
INTERESTED_MAJOR_NAMES_KEY = "interested_major_names"
POTENTIAL_MAJOR_NAMES_KEY = "potential_major_names"
PRIORITIZED_MAJOR_NAMES_KEY = "prioritized_major_names"
PRIMARY_MAJOR_NAME_KEY = "primary_major_name"
PRIMARY_MAJOR_SOURCE_KEY = "primary_major_source"
QUERY_REWRITE_SEGMENTS_KEY = "query_rewrite_segments"
QUERY_REWRITE_SEGMENT_COUNT_KEY = "query_rewrite_segment_count"
TRACE_MARKERS = (
    "For context:",
    "skipped by before_agent_callback",
    "called tool `",
    "tool returned result:",
)
TRACE_SAID_PATTERN = re.compile(r"\[[^\]]+\]\s*said:")
NUMBERED_LIST_ITEM_PATTERN = re.compile(r"^\s*(\d+)[\.\)]\s+(.+?)\s*$")
SHORT_ASCII_TOKEN_PATTERN = re.compile(r"^[a-z0-9&/\-]{1,6}$", re.IGNORECASE)


def _normalize_rewrite_results(raw_output: Any) -> list[dict[str, Any]]:
    """Normalize raw LLM output into a list of rewrite result items."""
    parsed_output = raw_output if isinstance(raw_output, (dict, list)) else _extract_json(str(raw_output))
    legacy_single_object = isinstance(parsed_output, dict)

    if legacy_single_object:
        parsed_output = [parsed_output]

    if not isinstance(parsed_output, list):
        raise ValueError("message_rewrite_result must be a JSON object or JSON array")

    normalized_results: list[dict[str, Any]] = []
    for idx, item in enumerate(parsed_output):
        if not isinstance(item, dict):
            raise ValueError(f"Rewrite item at index {idx} must be a JSON object")

        if "category" not in item or "rewritten_message" not in item:
            raise ValueError(f"Rewrite item at index {idx} must contain 'category' and 'rewritten_message' fields")

        if not legacy_single_object and "original_message" not in item:
            raise ValueError(f"Rewrite item at index {idx} must contain 'original_message'")

        category = item.get("category")
        original_message = item.get("original_message")
        rewritten_message = item.get("rewritten_message")

        if category not in VALID_CATEGORIES:
            raise ValueError(f"Rewrite item at index {idx} has invalid category: {category}")
        if original_message is not None and not isinstance(original_message, str):
            raise ValueError(f"Rewrite item at index {idx} has non-string original_message")
        if rewritten_message is not None and not isinstance(rewritten_message, str):
            raise ValueError(f"Rewrite item at index {idx} has non-string rewritten_message")

        normalized_results.append(
            {
                "original_message": original_message,
                "category": category,
                "rewritten_message": rewritten_message,
            }
        )

    return normalized_results


def _reset_rewrite_runtime_context(callback_context: CallbackContext) -> None:
    """Reset rewrite runtime state keys to empty values."""
    callback_context.state["message_rewrite_latest_user"] = None
    callback_context.state[CURRENT_USER_MESSAGE_KEY] = None
    callback_context.state[CURRENT_USER_RAW_MESSAGE_KEY] = None
    callback_context.state[LAST_USER_RAW_MESSAGE_KEY] = None
    callback_context.state[LAST_USER_EFFECTIVE_MESSAGE_KEY] = None
    callback_context.state["conversation_turn"] = []
    callback_context.state[LATEST_COMPLETED_AGENT_TURN_KEY] = []
    callback_context.state[LATEST_COMPLETED_ANSWER_TURN_KEY] = []
    callback_context.state[PREVIOUS_ANSWER_QUERY_RESPONSE_KEY] = None
    callback_context.state[LATEST_PREVIOUS_TURN_KEY] = []
    callback_context.state[LATEST_PREVIOUS_ANSWER_QUERY_RESPONSE_KEY] = None
    callback_context.state[LATEST_ANSWER_FOLLOW_UP_QUESTIONS_KEY] = []
    callback_context.state[RECENT_ANSWERED_MAJOR_NAMES_KEY] = []
    callback_context.state[INTERESTED_MAJOR_NAMES_KEY] = []
    callback_context.state[POTENTIAL_MAJOR_NAMES_KEY] = []
    callback_context.state[PRIORITIZED_MAJOR_NAMES_KEY] = []
    callback_context.state[PRIMARY_MAJOR_NAME_KEY] = None
    callback_context.state[PRIMARY_MAJOR_SOURCE_KEY] = None


def _unique_strings(values: list[Any]) -> list[str]:
    """Return stripped unique strings while preserving order."""
    unique_values: list[str] = []
    seen: set[str] = set()

    for value in values:
        text = str(value or "").strip()
        marker = text.casefold()
        if not text or marker in seen:
            continue
        seen.add(marker)
        unique_values.append(text)

    return unique_values


def _iter_state_major_items(raw_items: Any) -> list[tuple[str | None, str]]:
    """Extract major code/name pairs from flexible state payloads."""
    items = raw_items if isinstance(raw_items, list) else [raw_items] if raw_items else []
    extracted: list[tuple[str | None, str]] = []

    for item in items:
        if isinstance(item, list):
            extracted.extend(_iter_state_major_items(item))
            continue

        if not isinstance(item, dict):
            text = str(item or "").strip()
            if text:
                extracted.append((None, text))
            continue

        entity_name = item.get("entity_name") or item.get("name") or item.get("text")
        entity_code = item.get("entity_code") or item.get("code")
        if entity_name:
            extracted.append((str(entity_code).strip() if entity_code else None, str(entity_name).strip()))
            continue

        for entity_code_key, entity_name_value in item.items():
            name_text = str(entity_name_value or "").strip()
            if not name_text:
                continue
            code_text = str(entity_code_key).strip() if entity_code_key is not None else None
            extracted.append((code_text, name_text))

    deduped: list[tuple[str | None, str]] = []
    seen: set[tuple[str, str]] = set()
    for entity_code, entity_name in extracted:
        marker = ((entity_code or "").casefold(), entity_name.casefold())
        if marker in seen:
            continue
        seen.add(marker)
        deduped.append((entity_code, entity_name))

    return deduped


def _text_contains_candidate(text: str, candidate: str) -> bool:
    """Best-effort matcher for major codes/names inside short Vietnamese queries."""
    normalized_text = text.casefold()
    normalized_candidate = candidate.casefold().strip()
    if not normalized_candidate:
        return False

    if SHORT_ASCII_TOKEN_PATTERN.fullmatch(normalized_candidate):
        boundary_pattern = re.compile(rf"(?<![a-z0-9]){re.escape(normalized_candidate)}(?![a-z0-9])")
        return bool(boundary_pattern.search(normalized_text))

    return normalized_candidate in normalized_text


def _extract_recent_answered_major_names(
    previous_turn: dict[str, Any] | None,
    previous_raw_message: str | None,
    previous_effective_message: str | None,
    candidate_items: list[tuple[str | None, str]],
) -> list[str]:
    """Resolve major names from the immediately previous answered turn only."""
    if not isinstance(previous_turn, dict) or not previous_turn.get("answer_query_response"):
        return []

    candidate_texts = [
        previous_effective_message,
        previous_raw_message,
        previous_turn.get("user_message"),
    ]
    matched_names: list[str] = []

    for candidate_text in candidate_texts:
        if not isinstance(candidate_text, str) or not candidate_text.strip():
            continue

        for entity_code, entity_name in candidate_items:
            if _text_contains_candidate(candidate_text, entity_name):
                matched_names.append(entity_name)
                continue
            if entity_code and _text_contains_candidate(candidate_text, entity_code):
                matched_names.append(entity_name)

    return _unique_strings(matched_names)


def _extract_final_numbered_questions(answer_text: str | None) -> list[str]:
    """Extract the final trailing numbered follow-up question list from an answer."""
    if not isinstance(answer_text, str) or not answer_text.strip():
        return []

    lines = [line.rstrip() for line in answer_text.strip().splitlines()]
    collected_lines: list[str] = []

    for line in reversed(lines):
        stripped_line = line.strip()
        if not stripped_line:
            if collected_lines:
                break
            continue

        if not NUMBERED_LIST_ITEM_PATTERN.match(stripped_line):
            if collected_lines:
                break
            continue

        collected_lines.append(stripped_line)

    if not collected_lines:
        return []

    collected_lines.reverse()
    questions: list[str] = []
    expected_index = 1

    for line in collected_lines:
        match = NUMBERED_LIST_ITEM_PATTERN.match(line)
        if not match:
            return []

        index = int(match.group(1))
        if index != expected_index:
            return []

        questions.append(match.group(2).strip())
        expected_index += 1

    return _unique_strings(questions)


def _extract_text_from_event(event: Any) -> str | None:
    """Extract the first text part from an event if available."""
    if not event.content or not event.content.parts:
        return None

    part = event.content.parts[0]
    text = getattr(part, "text", None)
    if text is None:
        return None

    text = str(text)
    if not text.strip():
        return None

    return text


def _get_latest_user_event(callback_context: CallbackContext) -> tuple[int | None, Any | None, str | None]:
    """Return the latest user event index, its text part, and its raw text."""
    for idx in range(len(callback_context.session.events) - 1, -1, -1):
        event = callback_context.session.events[idx]
        event_author = getattr(event, "author", getattr(event, "role", "unknown"))
        if event_author != "user":
            continue

        text = _extract_text_from_event(event)
        if text is None:
            continue

        event_content = getattr(event, "content", None)
        event_parts = getattr(event_content, "parts", None)
        if isinstance(event_parts, list) and event_parts:
            return idx, event_parts[0], text

    return None, None, None


def _contains_context_trace(text: str | None) -> bool:
    """Detect whether a text looks like internal ADK/agent trace instead of user input."""
    if not isinstance(text, str):
        return False

    stripped = text.strip()
    if not stripped:
        return False

    if any(marker in stripped for marker in TRACE_MARKERS):
        return True

    return bool(TRACE_SAID_PATTERN.search(stripped))


def _has_invalid_trace_results(rewrite_results: list[dict[str, Any]]) -> bool:
    """Reject outputs that accidentally copied internal context into original_message."""
    return any(_contains_context_trace(result.get("original_message")) for result in rewrite_results)


def _get_previous_user_message_from_history(callback_context: CallbackContext) -> str | None:
    """Return the previous user message using the shared fast-history helper."""
    user_messages = get_fast_history(tool_context=callback_context, limit=2, author_filter="user")
    if len(user_messages) >= 2:
        return user_messages[-2]
    return None


def _get_latest_completed_turn_context(
    callback_context: CallbackContext, latest_user_text: str | None
) -> tuple[list[dict[str, str | None]], list[dict[str, str | None]]]:
    """Return the immediate previous turn and answer-bearing previous turn, if any."""
    conversation_turns = get_conversation_turns(callback_context, limit=5)
    if not conversation_turns:
        return [], []

    current_turn_index = None
    if latest_user_text:
        for idx in range(len(conversation_turns) - 1, -1, -1):
            if conversation_turns[idx].get("user_message") == latest_user_text:
                current_turn_index = idx
                break

    if current_turn_index is None:
        current_turn_index = len(conversation_turns) - 1

    if current_turn_index <= 0:
        return [], []

    previous_turn = conversation_turns[current_turn_index - 1]
    latest_completed_agent_turn = [previous_turn]
    latest_completed_answer_turn = [previous_turn] if previous_turn.get("answer_query_response") else []

    return latest_completed_agent_turn, latest_completed_answer_turn


def _prepare_major_runtime_hints(
    callback_context: CallbackContext,
    previous_turn: dict[str, Any] | None,
    previous_raw_message: str | None,
    previous_effective_message: str | None,
) -> None:
    """Populate ordered major hints used for ellipsis resolution."""
    state_dict = callback_context.state.to_dict()
    user_state = state_dict.get("user_state") if isinstance(state_dict.get("user_state"), dict) else {}
    interested_raw = user_state.get("interested_majors", []) or state_dict.get("interested_majors", [])
    potential_raw = user_state.get("potential_majors", []) or state_dict.get("potential_majors", [])
    if not potential_raw:
        potential_raw = user_state.get("major", []) or state_dict.get("user_major", [])

    interested_items = _iter_state_major_items(interested_raw)
    potential_items = _iter_state_major_items(potential_raw)
    candidate_items = interested_items + potential_items

    recent_answered_major_names = _extract_recent_answered_major_names(
        previous_turn=previous_turn,
        previous_raw_message=previous_raw_message,
        previous_effective_message=previous_effective_message,
        candidate_items=candidate_items,
    )
    interested_major_names = _unique_strings([entity_name for _, entity_name in interested_items])
    potential_major_names = _unique_strings([entity_name for _, entity_name in potential_items])
    prioritized_major_names = _unique_strings(
        recent_answered_major_names + interested_major_names + potential_major_names
    )

    primary_major_name = prioritized_major_names[0] if prioritized_major_names else None
    primary_major_source = None
    if primary_major_name:
        if primary_major_name in recent_answered_major_names:
            primary_major_source = "recent_answered_major_names"
        elif primary_major_name in interested_major_names:
            primary_major_source = "interested_majors"
        else:
            primary_major_source = "potential_majors"

    callback_context.state[RECENT_ANSWERED_MAJOR_NAMES_KEY] = recent_answered_major_names
    callback_context.state[INTERESTED_MAJOR_NAMES_KEY] = interested_major_names
    callback_context.state[POTENTIAL_MAJOR_NAMES_KEY] = potential_major_names
    callback_context.state[PRIORITIZED_MAJOR_NAMES_KEY] = prioritized_major_names
    callback_context.state[PRIMARY_MAJOR_NAME_KEY] = primary_major_name
    callback_context.state[PRIMARY_MAJOR_SOURCE_KEY] = primary_major_source


def _build_runtime_contract(callback_context: CallbackContext, latest_user_text: str) -> str:
    """Build the compact runtime context contract injected into the model input."""
    previous_turn = callback_context.state.get(LATEST_PREVIOUS_TURN_KEY)
    latest_previous_turn = previous_turn[0] if isinstance(previous_turn, list) and previous_turn else None

    conversation_context = {
        "current_user_message": latest_user_text,
        "last_user_raw_message": callback_context.state.get(LAST_USER_RAW_MESSAGE_KEY),
        "last_user_effective_message": callback_context.state.get(LAST_USER_EFFECTIVE_MESSAGE_KEY),
        "latest_previous_turn": latest_previous_turn,
        "latest_previous_answer_query_response": callback_context.state.get(LATEST_PREVIOUS_ANSWER_QUERY_RESPONSE_KEY),
        "previous_answer_query_response": callback_context.state.get(PREVIOUS_ANSWER_QUERY_RESPONSE_KEY),
    }
    major_hints = {
        "recent_answered_major_names": callback_context.state.get(RECENT_ANSWERED_MAJOR_NAMES_KEY, []),
        "interested_major_names": callback_context.state.get(INTERESTED_MAJOR_NAMES_KEY, []),
        "potential_major_names": callback_context.state.get(POTENTIAL_MAJOR_NAMES_KEY, []),
        "prioritized_major_names": callback_context.state.get(PRIORITIZED_MAJOR_NAMES_KEY, []),
        "primary_major_name": callback_context.state.get(PRIMARY_MAJOR_NAME_KEY),
        "primary_major_source": callback_context.state.get(PRIMARY_MAJOR_SOURCE_KEY),
    }
    follow_up_questions = callback_context.state.get(LATEST_ANSWER_FOLLOW_UP_QUESTIONS_KEY, []) or []
    numbered_follow_up_block = "\n".join(
        f"{idx}. {question}" for idx, question in enumerate(follow_up_questions, start=1)
    )

    return (
        "<runtime_context>\n"
        "<conversation_context>\n"
        f"{json.dumps(conversation_context, ensure_ascii=False, indent=2)}\n"
        "</conversation_context>\n"
        "<major_hints>\n"
        f"{json.dumps(major_hints, ensure_ascii=False, indent=2)}\n"
        "</major_hints>\n"
        "<latest_answer_follow_up_questions>\n"
        f"{numbered_follow_up_block or '[none]'}\n"
        "</latest_answer_follow_up_questions>\n"
        "</runtime_context>\n"
        "<task>\n"
        "Analyze only the latest user message using the runtime context above.\n"
        "Return the JSON array only.\n"
        "</task>"
    )


def _persist_last_user_context(
    callback_context: CallbackContext,
    raw_user_message: str | None,
    effective_user_message: str | None,
) -> None:
    """Persist the latest raw/effective user messages for the next rewrite turn."""
    if isinstance(raw_user_message, str) and raw_user_message.strip():
        callback_context.state[LAST_USER_RAW_MESSAGE_KEY] = raw_user_message
    else:
        callback_context.state[LAST_USER_RAW_MESSAGE_KEY] = None

    chosen_effective = (
        effective_user_message
        if isinstance(effective_user_message, str) and effective_user_message.strip()
        else raw_user_message
    )
    callback_context.state[LAST_USER_EFFECTIVE_MESSAGE_KEY] = chosen_effective


def before_rewrite_agent_callback(callback_context: CallbackContext) -> types.Content | None:
    """Prepare message rewrite state while preserving the shared skip logic."""
    callback_context.state["message_rewrite_invalid_reason"] = None
    callback_context.state[QUERY_REWRITE_SEGMENTS_KEY] = None
    callback_context.state[QUERY_REWRITE_SEGMENT_COUNT_KEY] = 0
    if callback_context.state.get(QR_ATTACHMENT_NOOP_STATE_KEY):
        callback_context.state["normalized_message_rewrite_result"] = []
        callback_context.state["final_rewritten_user_message"] = None
        callback_context.state[QUERY_REWRITE_SEGMENTS_KEY] = []
        callback_context.state[QUERY_REWRITE_SEGMENT_COUNT_KEY] = 0
        _reset_rewrite_runtime_context(callback_context)
        logger.info("[MessageRewriteAgent] Non-QR attachment without text -> skipping rewrite.")
        return types.Content(
            parts=[types.Part(text="Agent message_rewrite_agent skipped — non-QR attachment without text.")],
            role="model",
        )

    if callback_context.state.get(QR_ERROR_FAST_PATH_STATE_KEY):
        callback_context.state["normalized_message_rewrite_result"] = []
        callback_context.state["final_rewritten_user_message"] = None
        _reset_rewrite_runtime_context(callback_context)
        logger.info("[MessageRewriteAgent] QR fast-path detected -> skipping rewrite.")
        return types.Content(
            parts=[types.Part(text="Agent message_rewrite_agent skipped by QR fast-path.")],
            role="model",
        )

    skip_content = check_admission_topic_skip(callback_context)
    if skip_content is not None:
        _reset_rewrite_runtime_context(callback_context)
        return skip_content

    _, _, latest_user_text = _get_latest_user_event(callback_context)
    if latest_user_text is None:
        _reset_rewrite_runtime_context(callback_context)
        callback_context.state["normalized_message_rewrite_result"] = []
        callback_context.state["final_rewritten_user_message"] = None
        callback_context.state[QUERY_REWRITE_SEGMENTS_KEY] = []
        callback_context.state[QUERY_REWRITE_SEGMENT_COUNT_KEY] = 0
        logger.warning("[MessageRewriteAgent] Could not resolve latest user message before rewrite")
        return types.Content(
            parts=[types.Part(text="Agent message_rewrite_agent skipped — no text input found.")],
            role="model",
        )

    previous_raw_message = callback_context.state.get(LAST_USER_RAW_MESSAGE_KEY)
    if not isinstance(previous_raw_message, str) or not previous_raw_message.strip():
        previous_raw_message = _get_previous_user_message_from_history(callback_context)

    previous_effective_message = callback_context.state.get(LAST_USER_EFFECTIVE_MESSAGE_KEY)
    if not isinstance(previous_effective_message, str) or not previous_effective_message.strip():
        previous_effective_message = previous_raw_message

    previous_turn, latest_completed_answer_turn = _get_latest_completed_turn_context(callback_context, latest_user_text)
    previous_turn_payload = previous_turn[0] if previous_turn else None
    previous_answer_query_response = None
    if isinstance(previous_turn_payload, dict):
        previous_answer_query_response = previous_turn_payload.get("answer_query_response")
    latest_answer_follow_up_questions = _extract_final_numbered_questions(previous_answer_query_response)

    callback_context.state["message_rewrite_latest_user"] = latest_user_text
    callback_context.state[CURRENT_USER_MESSAGE_KEY] = latest_user_text
    callback_context.state[CURRENT_USER_RAW_MESSAGE_KEY] = latest_user_text
    callback_context.state[LAST_USER_RAW_MESSAGE_KEY] = previous_raw_message
    callback_context.state[LAST_USER_EFFECTIVE_MESSAGE_KEY] = previous_effective_message
    callback_context.state[LATEST_COMPLETED_AGENT_TURN_KEY] = previous_turn
    callback_context.state[LATEST_COMPLETED_ANSWER_TURN_KEY] = latest_completed_answer_turn
    callback_context.state[PREVIOUS_ANSWER_QUERY_RESPONSE_KEY] = previous_answer_query_response
    callback_context.state[LATEST_PREVIOUS_TURN_KEY] = previous_turn
    callback_context.state[LATEST_PREVIOUS_ANSWER_QUERY_RESPONSE_KEY] = previous_answer_query_response
    callback_context.state[LATEST_ANSWER_FOLLOW_UP_QUESTIONS_KEY] = latest_answer_follow_up_questions
    callback_context.state["conversation_turn"] = previous_turn
    _prepare_major_runtime_hints(
        callback_context=callback_context,
        previous_turn=previous_turn_payload,
        previous_raw_message=previous_raw_message,
        previous_effective_message=previous_effective_message,
    )

    logger.info(
        "[MessageRewriteAgent] Prepared rewrite context | current_user=%s | last_user_raw=%s | "
        "last_user_effective=%s | latest_previous_turn=%s | latest_completed_answer_turn=%s | "
        "previous_answer_query_response=%s | latest_answer_follow_up_questions=%s | prioritized_major_names=%s",
        latest_user_text,
        previous_raw_message,
        previous_effective_message,
        previous_turn,
        latest_completed_answer_turn,
        callback_context.state[PREVIOUS_ANSWER_QUERY_RESPONSE_KEY],
        callback_context.state[LATEST_ANSWER_FOLLOW_UP_QUESTIONS_KEY],
        callback_context.state[PRIORITIZED_MAJOR_NAMES_KEY],
    )

    return None


def before_rewrite_model_callback(callback_context: CallbackContext, llm_request: LlmRequest) -> LlmResponse | None:
    """Inject a compact runtime contract and the latest user text into the model input."""
    latest_user_text = callback_context.state.get("message_rewrite_latest_user")
    if not isinstance(latest_user_text, str) or not latest_user_text.strip():
        _, _, latest_user_text = _get_latest_user_event(callback_context)

    if not isinstance(latest_user_text, str) or not latest_user_text.strip():
        logger.warning("[MessageRewriteAgent] Missing latest user text before model call; keeping original request")
        return None

    runtime_contract = _build_runtime_contract(callback_context, latest_user_text)
    llm_request.contents = [
        types.Content(
            role="user",
            parts=[types.Part(text=runtime_contract)],
        )
    ]
    logger.info("[MessageRewriteAgent] Overrode llm_request.contents with runtime rewrite contract")
    return None


def _build_final_user_message(
    rewrite_results: list[dict[str, Any]], fallback_original_message: str | None
) -> str | None:
    """Build the final user message consumed by downstream agents."""
    final_parts: list[str] = []

    for idx, result in enumerate(rewrite_results):
        category = result["category"]
        original_message = result.get("original_message")
        rewritten_message = result.get("rewritten_message")

        if category in SKIP_REWRITE_CATEGORIES:
            selected_text = original_message
            if not selected_text and len(rewrite_results) == 1:
                selected_text = fallback_original_message
        else:
            selected_text = rewritten_message

        if not isinstance(selected_text, str) or not selected_text.strip():
            logger.warning(
                "[MessageRewriteAgent] Skipping invalid rewrite item at index %s: %s",
                idx,
                result,
            )
            continue

        text = selected_text.strip()
        if category == 6:
            text = f" {text}"
        final_parts.append(text)

    if not final_parts:
        return None

    return "\n".join(final_parts)


def _build_query_rewrite_segments(rewrite_results: list[dict[str, Any]]) -> list[str]:
    """Build downstream query-only segments from rewrite results."""
    query_segments: list[str] = []

    for result in rewrite_results:
        category = result["category"]
        if category in SKIP_REWRITE_CATEGORIES:
            if category != 2:
                continue
            selected_text = result.get("original_message")
        else:
            selected_text = result.get("rewritten_message")

        if not isinstance(selected_text, str) or not selected_text.strip():
            continue

        text = selected_text.strip()
        if category == 6:
            text = f" {text}"
        query_segments.append(text)

    return query_segments


def after_rewrite_agent_callback(callback_context: CallbackContext) -> None:
    """Parse JSON output and mutate the actual user message in session history if rewritten."""
    state_dict = callback_context.state.to_dict()
    raw_output = state_dict.get("message_rewrite_result", None)

    logger.info(f"[MessageRewriteAgent] Raw output: {raw_output}")

    _, user_part, original_text = _get_latest_user_event(callback_context)
    if user_part is None or original_text is None:
        logger.warning("[MessageRewriteAgent] Could not find latest user message in session history")
        return

    callback_context.state["original_user_message"] = original_text
    callback_context.state[QUERY_REWRITE_SEGMENTS_KEY] = None
    callback_context.state[QUERY_REWRITE_SEGMENT_COUNT_KEY] = 0

    if not raw_output:
        callback_context.state["final_rewritten_user_message"] = original_text
        _persist_last_user_context(callback_context, original_text, original_text)
        logger.warning("[MessageRewriteAgent] Empty rewrite output; keeping original user message")
        return

    try:
        rewrite_results = _normalize_rewrite_results(raw_output)
    except (json.JSONDecodeError, ValueError) as e:
        logger.error(f"[MessageRewriteAgent] Error parsing json: {e}")
        callback_context.state["final_rewritten_user_message"] = original_text
        callback_context.state["message_rewrite_invalid_reason"] = "parse_error"
        _persist_last_user_context(callback_context, original_text, original_text)
        return

    callback_context.state["normalized_message_rewrite_result"] = rewrite_results
    callback_context.state["message_rewrite_invalid_reason"] = None

    if _has_invalid_trace_results(rewrite_results):
        logger.warning(
            "[MessageRewriteAgent] Rejecting rewrite output because original_message contains internal trace: %s",
            rewrite_results,
        )
        callback_context.state["final_rewritten_user_message"] = original_text
        callback_context.state["message_rewrite_invalid_reason"] = "internal_trace_detected"
        _persist_last_user_context(callback_context, original_text, original_text)
        return

    final_user_message = _build_final_user_message(rewrite_results, original_text)
    query_rewrite_segments = _build_query_rewrite_segments(rewrite_results)
    callback_context.state["final_rewritten_user_message"] = final_user_message
    callback_context.state[QUERY_REWRITE_SEGMENTS_KEY] = query_rewrite_segments
    callback_context.state[QUERY_REWRITE_SEGMENT_COUNT_KEY] = len(query_rewrite_segments)

    logger.info(
        "[MessageRewriteAgent] Evaluated Rewrite Results: %s | Final User Message: %s | Query Segments: %s",
        rewrite_results,
        final_user_message,
        query_rewrite_segments,
    )

    if not final_user_message:
        logger.warning("[MessageRewriteAgent] No valid rewrite items found; keeping original user message")
        _persist_last_user_context(callback_context, original_text, original_text)
        return

    if final_user_message == original_text:
        logger.info("[MessageRewriteAgent] Final user message unchanged; skipping overwrite")
        _persist_last_user_context(callback_context, original_text, original_text)
        return

    user_part.text = final_user_message
    _persist_last_user_context(callback_context, original_text, final_user_message)
    logger.info(
        f"[MessageRewriteAgent] OVERWROTE User Event Text.\nOriginal: {original_text}\nNew:      {final_user_message}"
    )
