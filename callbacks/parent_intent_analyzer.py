"""Callbacks for Parent Intent Analyzer Agent.

Handles:
- before_agent_callback: Skip agent if is_admission_topic already set
- after_agent_callback: Parse output and set is_admission_topic + user_context_intent
- check_admission_topic_skip: Shared skip callback for all downstream agents
"""

import json
import logging
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.models import LlmRequest, LlmResponse
from google.genai import types

from agents.data_collector_agent import get_conversation_turns
from services.qr_support import (
    QR_ATTACHMENT_NOOP_STATE_KEY,
    QR_ERROR_FAST_PATH_STATE_KEY,
    sync_qr_fast_path_from_adk_user_content,
)
from utils.json_utils import extract_json as _extract_json

logger = logging.getLogger(__name__)

PARENT_INTENT_LATEST_USER_MESSAGE_KEY = "parent_intent_latest_user_message"
PARENT_INTENT_CONVERSATION_TURNS_KEY = "parent_intent_conversation_turns"
PARENT_INTENT_PREVIOUS_TURN_KEY = "parent_intent_previous_turn"
PARENT_INTENT_PREVIOUS_ANSWER_QUERY_RESPONSE_KEY = "parent_intent_previous_answer_query_response"


def _reset_parent_intent_runtime_context(callback_context: CallbackContext) -> None:
    callback_context.state[PARENT_INTENT_LATEST_USER_MESSAGE_KEY] = None
    callback_context.state[PARENT_INTENT_CONVERSATION_TURNS_KEY] = []
    callback_context.state[PARENT_INTENT_PREVIOUS_TURN_KEY] = []
    callback_context.state[PARENT_INTENT_PREVIOUS_ANSWER_QUERY_RESPONSE_KEY] = None


def _extract_text_from_event(event: Any) -> str | None:
    content = getattr(event, "content", None)
    parts = getattr(content, "parts", None)
    if not isinstance(parts, list) or not parts:
        return None

    text = getattr(parts[0], "text", None)
    if not isinstance(text, str):
        return None

    stripped_text = text.strip()
    return stripped_text if stripped_text else None


def _get_latest_user_message(callback_context: CallbackContext) -> str | None:
    for event in reversed(getattr(callback_context.session, "events", []) or []):
        event_author = getattr(event, "author", getattr(event, "role", "unknown"))
        if event_author != "user":
            continue

        text = _extract_text_from_event(event)
        if text:
            return text

    return None


def _serialize_runtime_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except TypeError:
        return str(value)


def _build_parent_intent_runtime_contract(callback_context: CallbackContext, latest_user_message: str) -> str:
    conversation_turns = callback_context.state.get(PARENT_INTENT_CONVERSATION_TURNS_KEY, [])
    previous_turn = callback_context.state.get(PARENT_INTENT_PREVIOUS_TURN_KEY, [])
    previous_answer = callback_context.state.get(PARENT_INTENT_PREVIOUS_ANSWER_QUERY_RESPONSE_KEY)

    return (
        "<runtime_context>\n"
        f"<current_user_message>{_serialize_runtime_value(latest_user_message)}</current_user_message>\n"
        f"<conversation_turns>{_serialize_runtime_value(conversation_turns)}</conversation_turns>\n"
        f"<previous_turn>{_serialize_runtime_value(previous_turn)}</previous_turn>\n"
        f"<previous_answer_query_response>{_serialize_runtime_value(previous_answer)}</previous_answer_query_response>\n"
        "</runtime_context>\n"
        "<task>\n"
        "Classify the current user message using the system rules. Return one strict JSON object only.\n"
        "</task>"
    )


def _prepare_parent_intent_runtime_context(callback_context: CallbackContext) -> bool:
    latest_user_message = _get_latest_user_message(callback_context)
    if not latest_user_message:
        _reset_parent_intent_runtime_context(callback_context)
        return False

    conversation_turns = get_conversation_turns(callback_context, limit=5)
    previous_turn = []
    previous_answer_query_response = None

    if conversation_turns:
        callback_context.state["conversation_turns"] = conversation_turns
        callback_context.state["conversation_turn"] = conversation_turns[-1:]
        current_turn_index = None
        for idx in range(len(conversation_turns) - 1, -1, -1):
            if conversation_turns[idx].get("user_message") == latest_user_message:
                current_turn_index = idx
                break

        if current_turn_index is None:
            current_turn_index = len(conversation_turns) - 1

        if current_turn_index > 0:
            previous_turn = [conversation_turns[current_turn_index - 1]]
            previous_answer_query_response = previous_turn[0].get("answer_query_response")
    else:
        callback_context.state["conversation_turns"] = []
        callback_context.state["conversation_turn"] = []

    callback_context.state[PARENT_INTENT_LATEST_USER_MESSAGE_KEY] = latest_user_message
    callback_context.state[PARENT_INTENT_CONVERSATION_TURNS_KEY] = conversation_turns
    callback_context.state[PARENT_INTENT_PREVIOUS_TURN_KEY] = previous_turn
    callback_context.state[PARENT_INTENT_PREVIOUS_ANSWER_QUERY_RESPONSE_KEY] = previous_answer_query_response
    logger.info(
        "[ParentIntentAnalyzer] Prepared context | latest_user=%s | conversation_turns=%s | previous_turn=%s | "
        "previous_answer_query_response=%s",
        latest_user_message,
        conversation_turns,
        previous_turn,
        previous_answer_query_response,
    )
    return True


def check_parent_intent_should_run(
    callback_context: CallbackContext,
) -> types.Content | None:
    """before_agent_callback for ParentIntentAnalyzerAgent.

    Runs continuously. Does not skip based on is_admission_topic anymore.
    Skips if user is blocked.
    """
    agent_name = callback_context.agent_name
    sync_qr_fast_path_from_adk_user_content(callback_context)
    state_dict = callback_context.state.to_dict()

    is_QnA_mode = state_dict.get("qna_mode", False)
    crm_update_state = state_dict.get("crm_update_state", False)
    if state_dict.get(QR_ATTACHMENT_NOOP_STATE_KEY):
        _reset_parent_intent_runtime_context(callback_context)
        logger.info(f"[{agent_name}] Non-QR attachment without text -> skipping parent intent analyzer.")
        return types.Content(
            parts=[types.Part(text=f"Agent {agent_name} skipped — non-QR attachment without text.")],
            role="model",
        )

    if state_dict.get(QR_ERROR_FAST_PATH_STATE_KEY):
        _reset_parent_intent_runtime_context(callback_context)
        logger.info(f"[{agent_name}] QR fast-path detected -> skipping parent intent analyzer.")
        return types.Content(
            parts=[types.Part(text=f"Agent {agent_name} skipped by QR fast-path.")],
            role="model",
        )

    is_blocked = state_dict.get("is_blocked", False)
    if is_blocked:
        _reset_parent_intent_runtime_context(callback_context)
        logger.info(f"[{agent_name}] User is blocked -> skipping.")
        return types.Content(
            parts=[types.Part(text=f"Agent {agent_name} skipped — user is blocked.")],
            role="model",
        )

    if is_QnA_mode and agent_name == "parent_intent_analyzer_agent":
        _reset_parent_intent_runtime_context(callback_context)
        return types.Content(
            parts=[types.Part(text=f"Agent {agent_name} skipped by before_agent_callback due to state.")],
            role="model",
        )

    if crm_update_state and agent_name == "parent_intent_analyzer_agent":
        _reset_parent_intent_runtime_context(callback_context)
        logger.info(f"[{agent_name}] skipped by before_agent_callback due to CRM update state..")
        return types.Content(
            parts=[types.Part(text=f"Agent {agent_name} skipped by before_agent_callback due to CRM update state.")],
            role="model",
        )

    if not _prepare_parent_intent_runtime_context(callback_context):
        logger.warning("[ParentIntentAnalyzer] Could not resolve latest user message before classification.")
        return types.Content(
            parts=[types.Part(text=f"Agent {agent_name} skipped — no text input found.")],
            role="model",
        )

    return None


def before_parent_intent_model_callback(
    callback_context: CallbackContext, llm_request: LlmRequest
) -> LlmResponse | None:
    latest_user_message = callback_context.state.get(PARENT_INTENT_LATEST_USER_MESSAGE_KEY)
    if not isinstance(latest_user_message, str) or not latest_user_message.strip():
        latest_user_message = _get_latest_user_message(callback_context)

    if not isinstance(latest_user_message, str) or not latest_user_message.strip():
        logger.warning("[ParentIntentAnalyzer] Missing latest user text before model call; keeping original request")
        return None

    runtime_contract = _build_parent_intent_runtime_contract(callback_context, latest_user_message.strip())
    llm_request.contents = [
        types.Content(
            role="user",
            parts=[types.Part(text=runtime_contract)],
        )
    ]
    logger.info("[ParentIntentAnalyzer] Overrode llm_request.contents with runtime contract")
    return None


def after_parent_intent_callback(
    callback_context: CallbackContext,
) -> None:
    """after_agent_callback for ParentIntentAnalyzerAgent.

    Reads the agent's JSON output from state["parent_intent_result"],
    then sets is_admission_topic and user_context_intent accordingly.

    Rules:
    - UC1 (ADM_*): set is_admission_topic=True, user_context_intent.append(intent_code)
    - UC2 (STU_*): set is_admission_topic=False, user_context_intent.append(intent_code),
                   clear extra_data
    - SPAM / null intent_code: do NOT touch is_admission_topic
    - GEN_OTHER: add to user_context_intent only if is_admission_topic is not set
    """
    state_dict = callback_context.state.to_dict()
    raw_output = state_dict.get("parent_intent_result", None)

    logger.info(f"[ParentIntentAnalyzer] after_callback raw output: {raw_output}")

    if not raw_output:
        logger.warning("[ParentIntentAnalyzer] No output found in parent_intent_result -> skipping.")
        return None

    # raw_output may be a dict already (ADK serializes JSON output_schema) or a string
    try:
        if isinstance(raw_output, dict):
            result = raw_output
        else:
            result = _extract_json(str(raw_output))
    except (json.JSONDecodeError, ValueError) as e:
        logger.error(f"[ParentIntentAnalyzer] Failed to parse output JSON: {e}")
        return None

    uc = result.get("uc", "UNKNOWN")
    intent_code = result.get("intent_code", None)

    logger.info(f"[ParentIntentAnalyzer] Parsed result -> uc={uc}, intent_code={intent_code}")

    current_intents = state_dict.get("user_context_intent", [])
    if isinstance(current_intents, str):
        current_intents = [current_intents] if current_intents else []
    elif not isinstance(current_intents, list):
        current_intents = []

    intent_to_add = None

    if intent_code and intent_code.startswith("ADM_MST_"):
        callback_context.state["uc"] = "UC3"
        intent_to_add = intent_code
        # Clear all extra_data (buttons, media, etc.) so nothing stale shows with transfer message
        callback_context.state["extra_data"] = {}
        logger.info(f"[ParentIntentAnalyzer] UC3 detected intent={intent_code}")
    elif intent_code and intent_code.startswith("ADM_VB2_"):
        callback_context.state["uc"] = "UC4"
        intent_to_add = intent_code
        # Clear all extra_data (buttons, media, etc.) so nothing stale shows with transfer message
        callback_context.state["extra_data"] = {}
        logger.info(f"[ParentIntentAnalyzer] UC4 detected intent={intent_code}")
    # Only set flag when we have a definitive ADM_* or STU_* intent_code
    elif intent_code and intent_code.startswith("ADM_"):
        callback_context.state["uc"] = "UC1"
        callback_context.state["is_admission_topic"] = True
        intent_to_add = intent_code
        logger.info(f"[ParentIntentAnalyzer] UC1 detected -> is_admission_topic=True, intent={intent_code}")

    elif intent_code and intent_code.startswith("STU_"):
        callback_context.state["uc"] = "UC2"
        callback_context.state["is_admission_topic"] = False
        intent_to_add = intent_code
        # Clear all extra_data (buttons, media, etc.) so nothing stale shows with transfer message
        callback_context.state["extra_data"] = {}
        logger.info(f"[ParentIntentAnalyzer] UC2 detected -> is_admission_topic=False, intent={intent_code}")

    elif uc == "SPAM":
        current_user_state = callback_context.state.get("user_state", {})
        if isinstance(current_user_state, dict):
            current_user_state["is_spam"] = True
            callback_context.state["user_state"] = current_user_state
        logger.warning(f"[ParentIntentAnalyzer] SPAM detected -> is_spam=True, intent={intent_code}")

    else:
        if "is_admission_topic" not in state_dict:
            if intent_code == "GEN_OTHER":
                intent_to_add = "GEN_OTHER"
        logger.info(f"[ParentIntentAnalyzer] uc={uc}, intent_code={intent_code} -> not setting is_admission_topic.")

    is_blocked_from_llm = result.get("is_blocked", False)
    if is_blocked_from_llm:
        callback_context.state["is_blocked"] = True
        callback_context.state["block_notified"] = False
        logger.warning("[ParentIntentAnalyzer] Community violation detected -> user blocked")

    if intent_to_add and intent_to_add not in current_intents:
        current_intents.append(intent_to_add)
        callback_context.state["user_context_intent"] = current_intents
        logger.info(f"[ParentIntentAnalyzer] Updated user_context_intent: {current_intents}")

    return None


def check_admission_topic_skip(
    callback_context: CallbackContext,
) -> types.Content | None:
    """Shared before_agent_callback for downstream agents.

    Skips the agent entirely when:
    - is_blocked is True (user is blocked)
    - is_admission_topic is False (UC2 user)
    Returns None (no skip) in all other cases.
    """
    agent_name = callback_context.agent_name
    state_dict = callback_context.state.to_dict()
    is_admission_topic = state_dict.get("is_admission_topic", None)
    conversation_turn = get_conversation_turns(callback_context, 1)
    crm_update_state = state_dict.get("crm_update_state", False)
    uc = callback_context.state.get("uc", None)
    callback_context.state["conversation_turn"] = conversation_turn

    is_blocked = state_dict.get("is_blocked", False)
    if is_blocked:
        logger.info(f"[AdmissionTopicSkip] is_blocked=True -> skipping agent {agent_name}.")
        return types.Content(
            parts=[types.Part(text=f"[{agent_name}] Skipped — user is blocked.")],
            role="model",
        )

    # UC2 skip disabled: let current-student/support questions continue through QA/RAG.
    if False and is_admission_topic is False:
        logger.info(f"[AdmissionTopicSkip] is_admission_topic=False -> skipping agent {agent_name}.")
        return types.Content(
            parts=[types.Part(text=f"[{agent_name}] Skipped — user is UC2 (current student).")],
            role="model",
        )

    if uc == "UC3" or uc == "UC4":
        logger.info(f"[AdmissionTopicSkip] uc={uc} -> skipping agent {agent_name}.")
        return types.Content(
            parts=[types.Part(text=f"Agent {agent_name} skipped — user is {uc}")],
            role="model",
        )

    if crm_update_state:
        logger.info(f"[{agent_name}] skipped by before_agent_callback due to CRM update state..")
        return types.Content(
            parts=[types.Part(text=f"Agent {agent_name} skipped by before_agent_callback due to CRM update state.")],
            role="model",
        )

    return None
