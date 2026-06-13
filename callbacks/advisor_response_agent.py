import json
from copy import deepcopy

import requests
from google.adk.agents.callback_context import CallbackContext
from google.adk.models import LlmResponse
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from configs.config_service import get_settings
from services.qr_support import QR_ATTACHMENT_NOOP_STATE_KEY, QR_ERROR_FAST_PATH_STATE_KEY
from utils.json_utils import extract_json
from utils.logging_config import get_logger

settings = get_settings()
logger = get_logger(__name__)


def check_if_agent_should_run(callback_context: CallbackContext) -> types.Content | None:
    """
    Logs entry and checks session state conditions.

    Skip conditions (in priority order):
    0. is_blocked=True → user is blocked, skip all agents using this callback
    1. is_admission_topic=False → UC2 user (current student), skip all processing agents
    2. qna_mode=True → skip data_collector_agent and segment_agent only
    """
    agent_name = callback_context.agent_name
    current_state = callback_context.state.to_dict()
    uc = current_state.get("uc", "")
    is_QnA_mode = current_state["qna_mode"] if "qna_mode" in current_state else False
    crm_update_state = current_state.get("crm_update_state", False)

    if current_state.get(QR_ATTACHMENT_NOOP_STATE_KEY) and agent_name in {"data_collector_agent", "segment_agent"}:
        logger.info(f"[Callback] Non-QR attachment without text -> skipping agent {agent_name}.")
        return types.Content(
            parts=[types.Part(text=f"Agent {agent_name} skipped — non-QR attachment without text.")],
            role="model",
        )

    if current_state.get(QR_ERROR_FAST_PATH_STATE_KEY) and agent_name in {"data_collector_agent", "segment_agent"}:
        logger.info(f"[Callback] QR fast-path detected  skipping agent {agent_name}.")
        return types.Content(
            parts=[types.Part(text=f"Agent {agent_name} skipped by QR fast-path.")],
            role="model",
        )

    is_blocked = current_state.get("is_blocked", False)
    if is_blocked:
        logger.warning(f"[Callback] is_blocked=True  skipping agent {agent_name} (user blocked).")
        return types.Content(
            parts=[types.Part(text=f"Agent {agent_name} skipped — user is blocked.")],
            role="model",
        )

    is_admission_topic = current_state.get("is_admission_topic", None)
    # UC2 segment skip disabled: let current-student/support questions continue downstream.
    if False and is_admission_topic is False and agent_name == "segment_agent":
        logger.info(f"[Callback] is_admission_topic=False  skipping agent {agent_name} (UC2 user).")
        return types.Content(
            parts=[types.Part(text=f"Agent {agent_name} skipped — user is UC2 (current student).")],
            role="model",
        )

    if is_QnA_mode and (agent_name == "data_collector_agent" or agent_name == "segment_agent"):
        return types.Content(
            parts=[types.Part(text=f"Agent {agent_name} skipped by before_agent_callback due to state.")],
            role="model",
        )

    if (uc == "UC3" or uc == "UC4") and agent_name == "segment_agent":
        logger.info(f"[AdmissionTopicSkip] skipping agent {agent_name}.")
        return types.Content(
            parts=[types.Part(text=f"Agent {agent_name} skipped — user is {uc}")],
            role="model",
        )
    if crm_update_state and agent_name == "segment_agent":
        logger.info(f"[{agent_name}] skipped by before_agent_callback due to CRM update state..")
        return types.Content(
            parts=[types.Part(text=f"Agent {agent_name} skipped by before_agent_callback due to CRM update state.")],
            role="model",
        )

    return None


def get_dmn_addressing_guide(tool_context: ToolContext, user_role: str | None | None = None) -> None:
    """
    Get DMN addressing guide based on user role in tool_context.state.
    """
    logger.debug("[Callback] Entering get_dmn_addressing_guide")
    current_state = tool_context.state.to_dict()
    user_role = user_role if user_role else current_state.get("customer_info", {}).get("customer_type", None)
    user_gender = current_state.get("customer_info", {}).get("gender", None)
    self_gender = current_state.get("sale_profile", {}).get("gender", None)
    payload = {"user_role": user_role, "user_gender": user_gender, "self_gender": self_gender}
    logger.info("get_dmn_addressing_guide payload: %s", payload)

    response = requests.post(
        f"{settings.kogito_endpoint_addressing_rule}/Addressing_rule",
        headers={"Content-Type": "application/json"},
        json=payload,
        timeout=5,
    )

    response.raise_for_status()

    data = response.json()
    addressing_guide = data.get("Addressing Role")
    logger.info("addressing_guide=%s", addressing_guide)
    tool_context.state.update(
        {"self_pronoun": addressing_guide.get("self_pronoun"), "user_pronoun": addressing_guide.get("user_pronoun")}
    )

    return None


def build_response_for_agent(callback_context: CallbackContext, llm_response: LlmResponse) -> LlmResponse | None:

    state = callback_context.state.to_dict()
    current_buttons = state.get("extra_data", {}).get("buttons", [])

    original_text = ""
    if llm_response.content and llm_response.content.parts:
        part = llm_response.content.parts[0]
        if getattr(part, "text", None):
            original_text = part.text

    logger.debug("build_response_for_agent callback_context: %s", original_text)

    if not original_text or not original_text.strip():
        logger.warning("Empty LLM response text")
        return llm_response

    try:
        original_json = extract_json(original_text)
    except json.JSONDecodeError:
        logger.exception("Invalid JSON from LLM")
        return llm_response

    final_answer = original_json.get("answer")
    cta = original_json.get("cta", [])

    if cta:
        current_buttons.extend(cta)
        callback_context.state["extra_data"]["buttons"] = current_buttons

    if final_answer:
        response_parts = getattr(getattr(llm_response, "content", None), "parts", None)
        if not isinstance(response_parts, list) or not response_parts:
            return llm_response

        modified_parts = [deepcopy(part) for part in response_parts]
        modified_parts[0].text = final_answer

        return LlmResponse(
            content=types.Content(role="model", parts=modified_parts),
            grounding_metadata=llm_response.grounding_metadata,
        )

    return llm_response
