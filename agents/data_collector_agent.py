import asyncio
import json
import logging
import random
import traceback
import unicodedata
from typing import Any

import nest_asyncio
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.llm_agent import LlmAgent
from google.adk.models import LlmRequest, LlmResponse
from google.adk.models.lite_llm import LiteLlm
from google.adk.tools.tool_context import ToolContext

from callbacks.advisor_response_agent import (
    check_if_agent_should_run,
    get_dmn_addressing_guide,
)
from callbacks.flatten_state_callbacks import flatten_state_callback
from configs.llm_client import get_litellm_config
from crud.offering import get_top_majors
from prompts.data_collector.data_collector import SYSTEM_PROMPT
from schemas.stage_variable import PersonalInfo, UserState
from schemas.submit_form import ApplicationFormData
from tools.get_cta_hint import ensure_user_state
from tools.industry_detector import VALID_INDUSTRIES, detect_industry_from_user_state
from tools.major_interest_scoring import apply_major_interest_scoring
from tools.playbook_hint_processing import (
    PlaybookDMNHintProcessor,
    check_missing_fields_user_profile,
    confirm_school_address,
    get_student_information_to_fill_form,
)
from tools.stage_detector import DMNStageDetectorProcessor
from tools.stage_extractor import stage_extractor
from tools.stage_progression import (
    TURN_INTENT_FIELDS,
    extract_turn_intent_flags,
    has_turn_intent,
    hydrate_empty_status,
    max_stage_status,
    normalize_turn_candidate_for_persist,
    should_use_turn_for_playbook,
)
from utils.submission_state import get_submission_like_route, get_submission_like_stage_status
from utils.submit_form import auto_submit_form

# from tools.stage_detector import
logger = logging.getLogger(__name__)
DATA_COLLECTOR_RAW_USER_INPUT_KEY = "_data_collector_raw_user_input"
CURRENT_USER_RAW_MESSAGE_KEY = "current_user_raw_message"


def _normalize_button_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFD", value)
    text = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
    text = text.replace("đ", "d").replace("Đ", "D").casefold()
    return " ".join(text.split())


def _is_fill_form_confirmation_button_click(user_message: str, buttons: Any, answer_guidance: Any) -> bool:
    if not isinstance(buttons, list):
        return False

    user_text = _normalize_button_text(user_message)
    matched_button = any(
        isinstance(button, dict)
        and user_text
        and user_text == _normalize_button_text(button.get("name"))
        and "xac nhan" in user_text
        for button in buttons
    )
    if not matched_button:
        return False

    guidance_text = _normalize_button_text(answer_guidance)
    return any(phrase in guidance_text for phrase in ("ho so", "dien form", "dang ky du tuyen", "mo ho so"))


def update_customer_info_using_user_state(tool_context: ToolContext, user_state: UserState):
    customer_info = tool_context.state.get("customer_info", {})
    logger.info(f"user_state.role: {user_state.role}")
    if user_state.role is None:
        return
    else:
        customer_info["customer_type"] = user_state.role if user_state.role else None
    logger.info(f"customer info: {customer_info}")


def _get_personal_info_field_names() -> tuple[str, ...]:
    if hasattr(PersonalInfo, "model_fields"):
        return tuple(PersonalInfo.model_fields.keys())
    return tuple(getattr(PersonalInfo, "__fields__", {}).keys())


def _extract_major_codes(major_list: list[dict[str, str]] | None) -> set[str]:
    major_codes = set()
    if not major_list:
        return major_codes

    for major in major_list:
        if not isinstance(major, dict):
            continue
        for major_code in major.keys():
            if major_code:
                major_codes.add(str(major_code))

    return major_codes


def _join_profile_mapping_codes(items: list[dict[str, str]] | None) -> list[str] | None:
    if not items:
        return None

    codes: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        for code in item.keys():
            code_text = str(code).strip()
            if code_text and code_text != "UNKNOWN" and code_text not in codes:
                codes.append(code_text)

    return codes if codes else None


def has_profile_or_major_changed(
    previous_state_raw: dict | UserState | None,
    current_state: dict | UserState | None,
) -> bool:
    previous_state = ensure_user_state(previous_state_raw)
    current_state_obj = ensure_user_state(current_state)

    previous_profile = previous_state.student_profile or PersonalInfo()
    current_profile = current_state_obj.student_profile or PersonalInfo()

    for field_name in _get_personal_info_field_names():
        if getattr(previous_profile, field_name, None) != getattr(current_profile, field_name, None):
            return True

    previous_major_codes = _extract_major_codes(previous_state.major)
    current_major_codes = _extract_major_codes(current_state_obj.major)

    return previous_major_codes != current_major_codes


def hydrate_current_status_after_playbook(tool_context: ToolContext, rule_id: Any) -> None:
    if not rule_id:
        return

    state = tool_context.state.to_dict()

    current_status = state.get("current_status")
    if not (isinstance(current_status, str) and current_status == ""):
        return

    hydrated_status = hydrate_empty_status(state.get("current_stage"), current_status)
    if hydrated_status != current_status:
        tool_context.state.update(
            {
                "current_status": hydrated_status,
            }
        )


def remove_ephemeral_other_topic(topics: list[str] | None) -> list[str]:
    if not topics:
        return []

    cleaned_topics = []
    for topic in topics:
        if isinstance(topic, str) and topic.strip().lower() == "khác":
            continue
        cleaned_topics.append(topic)

    return cleaned_topics


def has_student_profile_contact(student_profile: dict[str, Any] | None) -> bool:
    if not isinstance(student_profile, dict):
        return False

    phone = student_profile.get("phone")
    email = student_profile.get("email")
    return bool(
        (isinstance(phone, str) and phone.strip())
        or (isinstance(email, str) and email.strip())
    )


def cleanup_ephemeral_topics_after_turn(
    context: CallbackContext | dict[str, Any],
    state_obj: UserState,
) -> UserState:
    if hasattr(context, "state"):
        state_dict = context.state.to_dict()
    else:
        state_dict = context
    persisted_topics = remove_ephemeral_other_topic(state_dict.get("user_state", {}).get("topic"))

    if "user_state" in state_dict and isinstance(state_dict["user_state"], dict):
        state_dict["user_state"]["topic"] = persisted_topics

    state_obj.topic = persisted_topics

    if "user_topic" in state_dict:
        state_dict["user_topic"] = str(persisted_topics)

    return state_obj


def get_fast_history(
    tool_context: ToolContext | CallbackContext, limit=1, author_filter: str = "agent", agent_name: str | None = None
):
    """
    Get recent messages from conversation history.

    Args:
        tool_context: ToolContext with session events
        limit: Maximum number of messages to return
        author_filter:
            - "agent": Only return agent messages (author != "user")
            - "user": Only return user messages (author == "user")
            - "all": Return all messages regardless of author
        agent_name: Optional name of the specific agent to filter by.
                   If provided, overrides author_filter.

    Returns:
        List of message texts, in chronological order (oldest first)
    """
    history = []
    logger.debug(f"Debugging get_fast_history: limit={limit}, filter={author_filter}, agent_name={agent_name}")

    for event in reversed(tool_context.session.events):
        if not event.content:
            continue

        # Get the author of this event
        event_author = getattr(event, "author", None)
        if event_author is None:
            # Fallback to role if author is missing
            event_author = getattr(event, "role", "unknown")

        # logger.error(
        #     f"Event: author={event_author}, content_len={len(event.content.parts) if event.content.parts else 0}"
        # )

        if agent_name:
            if event_author != agent_name:
                continue
        else:
            # Apply author filter
            if author_filter == "agent" and event_author == "user":
                continue  # Skip user messages when looking for agent messages
            elif author_filter == "user" and event_author != "user":
                continue  # Skip agent messages when looking for user messages
            # author_filter == "all" includes everything

        # Check for text content
        if event.content.parts:
            part = event.content.parts[0]
            if hasattr(part, "text") and part.text:
                history.append(part.text.strip())

        if len(history) == limit:
            break

    return list(reversed(history))


def get_conversation_turns(tool_context, limit=5):
    """Build turn-based conversation history from session events.

    Each turn starts when a 'user' event appears.
    Agent responses following that user event belong to the same turn.

    Returns:
        List[Dict] ordered oldest→newest, max `limit` turns.
        Each dict: {user_message, answer_query_response, counselor_playbook_response}
    """
    turns = []
    current_turn = None

    for event in tool_context.session.events:
        if not event.content or not event.content.parts:
            continue

        author = getattr(event, "author", None) or getattr(event, "role", "unknown")
        part = event.content.parts[0]
        if not hasattr(part, "text") or not part.text:
            continue
        text = part.text.strip()
        if not text:
            continue

        if author == "user":
            # New turn starts
            current_turn = {
                "user_message": text,
                "answer_query_response": None,
                "counselor_playbook_response": None,
            }
            turns.append(current_turn)
        elif current_turn is not None:
            if author == "answer_query_agent":
                current_turn["answer_query_response"] = text
            elif author == "counselor_playbook_agent":
                current_turn["counselor_playbook_response"] = text

    # Return last `limit` turns
    return turns[-limit:]


def _get_latest_user_message(tool_context: ToolContext | CallbackContext) -> str | None:
    history_messages = get_fast_history(tool_context=tool_context, limit=1, author_filter="user")
    if history_messages:
        return history_messages[0]
    return None


def _get_current_turn_index(tool_context: ToolContext | CallbackContext) -> int:
    user_context = tool_context.state.get("user_context", {})
    try:
        current_turn_index = int(user_context.get("message_count", 0) or 0)
    except (TypeError, ValueError):
        current_turn_index = 0
    return max(current_turn_index, 1)


def _strip_markdown_fences(content: str) -> str:
    if "```json" in content:
        content = content.replace("```json", "").replace("```", "")
    elif "```" in content:
        content = content.replace("```", "")
    return content.strip()


def data_collector_before_model_callback(
    callback_context: ToolContext | CallbackContext, llm_request: LlmRequest
) -> LlmResponse | None:
    raw_user_input = callback_context.state.get(CURRENT_USER_RAW_MESSAGE_KEY)
    if not isinstance(raw_user_input, str) or not raw_user_input.strip():
        raw_user_input = _get_latest_user_message(callback_context)
    callback_context.state[DATA_COLLECTOR_RAW_USER_INPUT_KEY] = raw_user_input

    if raw_user_input:
        logger.info(f"[DataCollector] Snapshotted raw user input: {raw_user_input}")
    else:
        logger.warning("[DataCollector] Could not snapshot raw user input from session history")

    return None


def _run_data_collector_from_callback(callback_context: CallbackContext, user_message: str):
    async def _run():
        return await data_collector_agent(tool_context=callback_context, user_message=user_message)

    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        running_loop = None

    if running_loop and running_loop.is_running():
        nest_asyncio.apply()
        return running_loop.run_until_complete(_run())

    try:
        return asyncio.run(_run())
    except RuntimeError:
        loop = asyncio.get_event_loop()
        nest_asyncio.apply()
        return loop.run_until_complete(_run())


def data_collector_after_model_callback(
    callback_context: CallbackContext, llm_response: LlmResponse
) -> LlmResponse | None:
    if llm_response is not None and getattr(llm_response, "partial", None) not in (False, None):
        logger.info("[DataCollector] Skipping partial LLM response")
        return None

    raw_user_input = callback_context.state.get(DATA_COLLECTOR_RAW_USER_INPUT_KEY)
    if not raw_user_input:
        raw_user_input = callback_context.state.get(CURRENT_USER_RAW_MESSAGE_KEY)
    if not raw_user_input:
        raw_user_input = _get_latest_user_message(callback_context)
        if raw_user_input:
            callback_context.state[DATA_COLLECTOR_RAW_USER_INPUT_KEY] = raw_user_input

    raw_content = None
    response_content = getattr(llm_response, "content", None)
    response_parts = getattr(response_content, "parts", None)
    if isinstance(response_parts, list) and response_parts:
        first_part = response_parts[0]
        raw_content = getattr(first_part, "text", None)

    if raw_content:
        try:
            payload = json.loads(_strip_markdown_fences(raw_content))
            is_valid_payload = isinstance(payload, dict) and payload == {"should_collect": True}
            if is_valid_payload:
                logger.info("[DataCollector] Valid callback payload received: {'should_collect': true}")
            else:
                logger.error(f"[DataCollector] Unexpected payload shape {payload!r}; fail-open to collector execution")
        except Exception as e:
            logger.error(f"[DataCollector] Failed to parse callback payload: {e}; fail-open to collector execution")
    else:
        logger.warning("[DataCollector] Empty model payload; fail-open to collector execution")

    if not raw_user_input:
        logger.warning("[DataCollector] No raw user input available; skipping collector execution")
        return None

    try:
        _run_data_collector_from_callback(callback_context, raw_user_input)
    except Exception as e:
        logger.error(f"[DataCollector] Collector execution failed: {e}")
        traceback.print_exc()

    return None


async def data_collector_agent(
    tool_context: CallbackContext,
    user_message: str,
) -> dict[str, int | bool | dict | dict[Any, Any] | Any] | None:
    """Data collector - extracts state and generates CTAs.

    Flow:
    1. stage_extractor(user_input) → state
    2. stage_detector(state) + get_cta_hint(state) → stage, cta_hint
    3. cta_generator(conversation, cta_hint, stage) → CTAs

    Args:
        tool_context: ADK ToolContext with state
        user_message: User's input message

    Returns:
        Stage progression info with CTAs
    """
    try:
        current_stage = tool_context.state.get("current_stage", None)
        current_status = tool_context.state.get("current_status", None)
        last_rule_id = tool_context.state.get("last_rule_id", 1)
        last_rule_id_count = tool_context.state.get("last_rule_id_count", 0)

        if user_message and user_message.strip():
            logger.info(f"Using callback-provided user input: {user_message}")
        else:
            history_messages = get_fast_history(tool_context=tool_context, limit=1, author_filter="user")
            if history_messages:
                user_message = history_messages[0]
                logger.info(f"Retrieved latest_user_input from history fallback: {user_message}")
            else:
                logger.warning("Could not fetch user message from history fallback; using original argument as-is")

        # SAVE USER INPUT TO STATE
        tool_context.state["latest_user_input"] = user_message

        logger.info(f"User message: {user_message}")
        customer_info = tool_context.state.get("customer_info", {})

        state_dict = tool_context.state

        current_extra_data = state_dict.get("extra_data", {})
        matched_fill_form_confirmation_button = _is_fill_form_confirmation_button_click(
            user_message=user_message,
            buttons=current_extra_data.get("buttons") if isinstance(current_extra_data, dict) else None,
            answer_guidance=state_dict.get("playbook_answer_guidance"),
        )
        current_extra_data["buttons"] = []  # Clear to empty list

        state_dict["extra_data"] = current_extra_data  # Explicit re-assign

        previous_state_raw = (
            state_dict.get("user_state")
            if "user_state" in state_dict
            else UserState(role=customer_info.get("customer_type", None), topic=[])
        )
        logger.info(f"previous_state_raw: {previous_state_raw}")
        previous_state = ensure_user_state(previous_state_raw)
        logger.info(f"previous_state: {previous_state}")
        previous_is_spam = previous_state.is_spam if hasattr(previous_state, "is_spam") else False
        # logger.error("before call stage_extractor")
        current_role = previous_state.role
        # Get the most recent agent messages separately
        answer_msgs = get_fast_history(tool_context=tool_context, limit=1, agent_name="answer_query_agent")
        answer_msg = answer_msgs[0] if answer_msgs else None

        playbook_msgs = get_fast_history(tool_context=tool_context, limit=1, agent_name="counselor_playbook_agent")
        playbook_msg = playbook_msgs[0] if playbook_msgs else None

        # Combine into an object for stage_extractor context (needs only 1 msg)
        last_agent_message = {"answer_query_agent": answer_msg, "counselor_playbook_agent": playbook_msg}
        tool_context.state["latest_user_messages"] = user_message

        # user_message Is now a string (from argument), not a list
        stage_extractor_output = stage_extractor(
            user_input=user_message,
            current_stage=previous_state,
            last_agent_message=last_agent_message,
            return_metadata=True,
        )
        if isinstance(stage_extractor_output, tuple):
            user_state, extractor_metadata = stage_extractor_output
        else:
            user_state, extractor_metadata = stage_extractor_output, {}

        if tool_context.state.get("playbook_terminal_refusal_active", False):
            tool_context.state["playbook_terminal_refusal_active"] = False

        if tool_context.state.get("playbook_force_spam", False):
            current_not_follow_count = int(tool_context.state.get("playbook_not_follow_count", 0) or 0)
            tool_context.state["playbook_not_follow_count"] = current_not_follow_count + 1
            logger.info(
                "[DataCollector] playbook_force_spam=True -> incrementing playbook_not_follow_count to %s.",
                tool_context.state["playbook_not_follow_count"],
            )

        if matched_fill_form_confirmation_button:
            logger.info("[DataCollector] Matched fill-form confirmation button; forcing confirmation flag")
            user_state.is_confirm_information_to_fill_form = True
            user_state.is_requested_submit_application_now = False

        logger.info(f"after call stage_extractor: {user_state}")
        spam_count = tool_context.state.get("spam_count", 0)
        current_is_spam = bool(user_state.is_spam)
        is_asking_agent_identity = user_state.is_asking_agent_identity

        if previous_is_spam and current_is_spam:
            spam_count += 1
        elif current_is_spam:
            spam_count = 1
        else:
            spam_count = 0

        tool_context.state["spam_count"] = spam_count

        # Normalize to UserState object for downstream tools
        state_obj = ensure_user_state(user_state)
        conversation_turns = tool_context.state.get("conversation_turns", None)
        if not conversation_turns:
            conversation_turns = get_conversation_turns(tool_context, limit=5)
            if conversation_turns:
                tool_context.state["conversation_turns"] = conversation_turns
        current_turn_index = _get_current_turn_index(tool_context)
        turn_major_entities = extractor_metadata.get("turn_major_entities", []) if isinstance(extractor_metadata, dict) else []
        tool_context.state["turn_major_entities"] = turn_major_entities
        state_obj = apply_major_interest_scoring(
            current_state=state_obj,
            previous_state=previous_state,
            user_message=user_message,
            last_agent_message=last_agent_message.get("counselor_playbook_agent"),
            conversation_turns=conversation_turns,
            turn_entities=turn_major_entities,
            current_turn_index=current_turn_index,
        )
        update_customer_info_using_user_state(tool_context, state_obj)
        # current_customer_info = tool_context.state.get('customer_info', {})
        # logger.error(f"current_customer_info: {current_customer_info}")
        logger.info(f"current_role: {current_role}")
        if (
            current_role != user_state.role
            or (current_role, user_state.role) == (None, None)
            or "self_pronoun" not in tool_context.state
            or current_role is None
        ):
            logger.warning(f"Current role: {user_state.role} and previous role: {current_role}")
            get_dmn_addressing_guide(tool_context, user_role=user_state.role)
        state_dict["user_state"] = state_obj.model_dump()
        logger.info(f"user_state: {state_dict['user_state']}")
        flatten_state_callback(tool_context)
        confirm_school_address(tool_context)
        dmn_stage_detector = DMNStageDetectorProcessor()
        turn_intent_flags = extract_turn_intent_flags(state_obj)
        durable_intent_override = {field: False for field in TURN_INTENT_FIELDS}
        durable_candidate = dmn_stage_detector.stage_detector(tool_context, intent_override=durable_intent_override)
        turn_candidate = dmn_stage_detector.stage_detector(tool_context)
        base_journey = max_stage_status(
            [
                (current_stage, current_status),
                durable_candidate,
            ]
        )
        persist_turn_candidate = normalize_turn_candidate_for_persist(turn_candidate, turn_intent_flags)
        next_stage, next_status = max_stage_status(
            [
                base_journey,
                persist_turn_candidate,
            ]
        )
        state_dict["current_stage"] = next_stage
        state_dict["current_status"] = next_status
        is_changed_personal_info = has_profile_or_major_changed(
            previous_state_raw=previous_state_raw, current_state=state_obj
        )
        logger.info(f"is_changed_personal_info: {is_changed_personal_info}")
        skip_count = 1
        # state_dict["lead_stage"] = next_stage
        # state_dict["lead_status"] = next_status
        submission_route = get_submission_like_route(tool_context.state.to_dict())
        if submission_route is None and not is_asking_agent_identity and not current_is_spam:
            # Reset playbook states to prevent stale data from showing up in subsequent stages
            if not tool_context.state.get("is_pending_school_confirmation", False):
                tool_context.state["playbook_action_results"] = None
                tool_context.state["playbook_action_instruction"] = None

            is_requested_submit_application_now = state_dict["user_state"].get("is_requested_submit_application_now", False)
            is_confirm_information_to_fill_form = state_dict["user_state"].get("is_confirm_information_to_fill_form", False)
            logger.info(f"is_requested_submit_application_now: {is_requested_submit_application_now}")
            if is_requested_submit_application_now or is_confirm_information_to_fill_form:
                logger.info(f"is_requested_submit_application_now should go there: {is_requested_submit_application_now}")
                missing_base_information_field = check_missing_fields_user_profile(state_dict.to_dict())
                interested_majors = state_dict["user_state"].get("interested_majors", [])
                potential_majors = state_dict["user_state"].get("potential_majors", [])
                if not interested_majors and not potential_majors:
                    interested_majors = state_dict["user_state"].get("majors", [])
                logger.critical(f"danger 1: {missing_base_information_field} and {interested_majors}")
                if len(missing_base_information_field) == 0 and len(interested_majors) > 0:
                    # logger.critical(f"danger 2: {missing_base_information_field} and {interested_majors}")
                    enrollment_data = get_student_information_to_fill_form(state_dict.to_dict()) or {}
                    grade12_school = None
                    crm_school_id = state_dict.get("crm_school_id")
                    if enrollment_data.get("student_high_school") is not None:
                        if crm_school_id:
                            grade12_school = int(crm_school_id[0])

                    majors = enrollment_data.get("major") or []
                    # logger.error(f"CTA majors: {majors}")
                    specialization_code = []
                    majors_code = []
                    for major in majors:
                        code = list(major.keys())[0]
                        if "-" in code:
                            specialization_code.append(code)
                        else:
                            majors_code.append(code)
                    admission_method_codes = _join_profile_mapping_codes(enrollment_data.get("admission_methods"))

                    form_data = ApplicationFormData(
                        full_name=enrollment_data.get("student_name"),
                        student_phone=enrollment_data.get("student_phone"),
                        email=enrollment_data.get("student_email"),
                        grade12_school=grade12_school,
                        grade12_province=enrollment_data.get("student_high_school_province"),
                        section_id=tool_context.session.id,
                        majors=",".join(majors_code),
                        specializations=",".join(specialization_code) if specialization_code else None,
                        admission_methods=admission_method_codes,
                    )
                    submit_success, message_from_crm = auto_submit_form(form_data)
                    logger.info(f"submit_success: {submit_success}")
                    if submit_success:
                        tool_context.state["skip_playbook_scenario"] = True
                    else:
                        tool_context.state["answer_while_got_error"] = message_from_crm
                        tool_context.state["skip_playbook_scenario"] = True
            else:
                playbook_dmn_hint_processor = PlaybookDMNHintProcessor()
                should_use_turn_playbook = has_turn_intent(turn_intent_flags) and should_use_turn_for_playbook(
                    turn_candidate, base_journey
                )
                if has_turn_intent(turn_intent_flags) and not should_use_turn_playbook:
                    tool_context.state["playbook_turn_intent_override"] = {field: False for field in TURN_INTENT_FIELDS}
                else:
                    tool_context.state["playbook_turn_intent_override"] = None

                if should_use_turn_playbook:
                    turn_stage = turn_candidate.get("next_stage") if isinstance(turn_candidate, dict) else None
                    turn_status = turn_candidate.get("next_status") if isinstance(turn_candidate, dict) else None
                    tool_context.state["playbook_stage_override"] = turn_stage
                    tool_context.state["playbook_status_override"] = turn_status
                else:
                    tool_context.state["playbook_stage_override"] = None
                    tool_context.state["playbook_status_override"] = None

                try:
                    rule_id, skip_count = playbook_dmn_hint_processor.process_hints(tool_context)
                finally:
                    tool_context.state["playbook_turn_intent_override"] = None
                    tool_context.state["playbook_stage_override"] = None
                    tool_context.state["playbook_status_override"] = None
                hydrate_current_status_after_playbook(tool_context, rule_id)
                logger.info(f"rule_id: {rule_id}")
                if rule_id == last_rule_id and not is_changed_personal_info:
                    last_rule_id_count += 1
                else:
                    tool_context.state["skip_playbook_scenario"] = False
                    logger.info(
                        "[DataCollector] rule changed from %s to %s -> resetting playbook_not_follow_count and playbook_force_spam.",
                        last_rule_id,
                        rule_id,
                    )
                    tool_context.state["playbook_not_follow_rule_id"] = rule_id
                    tool_context.state["playbook_not_follow_count"] = 0
                    tool_context.state["playbook_force_spam"] = False
                    last_rule_id = rule_id
                    last_rule_id_count = 0

                tool_context.state["last_rule_id"] = last_rule_id
                tool_context.state["last_rule_id_count"] = last_rule_id_count
                # if last_rule_id_count > 3:
                #     logger.error(f"Rule '{rule_id}' exceeded 3 consecutive times")
                #     state_dict["user_state"]["is_spam"] = True

                # logger.debug(f"stage_extractor type of state_obj: {type(state_obj)}")

                # logger.debug(f"Getting CTA hint with state: {state_dict.get('user_state')}")
        elif submission_route is not None:
            state_dict["current_stage"], state_dict["current_status"] = get_submission_like_stage_status(submission_route)
            tool_context.state["skip_playbook_scenario"] = True
        else:
            tool_context.state["skip_playbook_scenario"] = True
        # skip_threshold = skip_count
        if last_rule_id_count >= skip_count:
            tool_context.state["skip_playbook_scenario"] = True
        cleanup_ephemeral_topics_after_turn(tool_context, state_obj)
        user_state = ensure_user_state(state_dict.get("user_state"))
        user_has_major = hasattr(user_state, "major") and user_state.major
        is_requested_advise_major = (
            hasattr(user_state, "is_requested_advise_major") and user_state.is_requested_advise_major is not None
        )
        logger.info(f"is_requested_advise_major: {is_requested_advise_major}")
        logger.info(f"user_has_major: {user_has_major}")

        if user_has_major:
            if "content_offerings_text" not in state_dict:
                # Extract major names from List[Dict[str, str]]
                # each m is {code: name}
                major_names = []
                for m in user_state.major:
                    if m:
                        major_names.extend(list(m.values()))
                major_codes = _join_profile_mapping_codes(user_state.major)

                logger.info(f"Fetching top majors for user major: {major_names}")

                try:
                    detected_industries = detect_industry_from_user_state(major_names)
                except Exception as e:
                    logger.error(f" Exception during industry detection: {e}")
                    detected_industries = None

                if isinstance(detected_industries, list) and detected_industries:
                    industries = detected_industries
                else:
                    industries = [random.choice(VALID_INDUSTRIES)]

                # Call get_top_majors API
                offering_response = get_top_majors(
                    expected_industries=industries,
                    major_codes=major_codes,
                    score=None,
                    top_k=3,
                )

                if offering_response:
                    major_list = offering_response.majors
                    club_list = offering_response.clubs

                    # Add to result for response_polishing_agent
                    state_dict["content_offerings_text"] = str(
                        {
                            "recommended_majors": [
                                {
                                    "major_id": major.major_id,
                                    "major_name": major.major_name,
                                    "admission_rate": major.successful_admission_rates,
                                    "salary_range": major.trend_info.salary_range,
                                    "market_demand": major.trend_info.market_demand,
                                }
                                for major in major_list
                            ]
                            if major_list
                            else [],
                            "recommended_clubs": club_list or [],
                        }
                    )

                else:
                    logger.warning(" No majors returned from get_top_majors API")

        # tool_context.actions.skip_summarization = True
        return user_state.model_dump()
    except Exception:
        traceback.print_exc()


_cfg = get_litellm_config(task="gatekeeper")
data_collector_agent_for_root = LlmAgent(
    model=LiteLlm(**_cfg),
    name="data_collector_agent",
    instruction=SYSTEM_PROMPT,
    before_agent_callback=check_if_agent_should_run,
    before_model_callback=data_collector_before_model_callback,
    after_model_callback=data_collector_after_model_callback,
    include_contents="none",
)
logger.info(" Data Collector Agent initialized")
