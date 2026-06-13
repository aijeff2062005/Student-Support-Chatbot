import ast
import json
import logging
import random
import re
import traceback
from collections.abc import Callable
from enum import StrEnum
from typing import Any

import requests
from google.adk.tools.tool_context import ToolContext

from configs.config_service import get_settings
from dbs.career_recommendation_ops import search_career_recommendation_candidates
from dbs.graph_search_helpers import sanitize_neo4j_types
from dbs.milvus_helper import search_school_hybrid
from schemas.submit_form import ApplicationFormData
from tools.QA.services.neo4j_service import Neo4jService
from tools.stage_progression import TURN_INTENT_FIELDS, extract_turn_intent_flags
from utils.handle_action_hint_dmn import handle_action_type_query
from utils.province_normalizer import normalize_province
from utils.submit_form import submit_application_form

settings = get_settings()

logger = logging.getLogger("playbook_dmn_hint_processor")


def _has_contact_info(state: dict[str, Any]) -> bool:
    if bool(state.get("has_contact_info", False)):
        return True

    student_phone = state.get("student_phone")
    student_email = state.get("student_email")
    student_profile = state.get("user_state", {}).get("student_profile", {})
    if not student_phone and isinstance(student_profile, dict):
        student_phone = student_profile.get("phone")
    if not student_email and isinstance(student_profile, dict):
        student_email = student_profile.get("email")

    return bool(
        (isinstance(student_phone, str) and student_phone.strip())
        or (isinstance(student_email, str) and student_email.strip())
    )


def find_object_by_key(data: list[dict], key: str):
    return next((obj for obj in data if key in obj), None)


def _normalize_button_payload(button: Any) -> dict[str, Any] | None:
    """Normalize a button payload so merge/dedupe stays stable across sources."""
    if not isinstance(button, dict):
        return None

    normalized = {}
    for key, value in button.items():
        normalized[key] = None if key == "link" and value in ("", None) else value
    return normalized


def _merge_buttons(existing_buttons: Any, new_buttons: Any) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()

    existing_list = existing_buttons if isinstance(existing_buttons, list) else []
    new_list = new_buttons if isinstance(new_buttons, list) else []

    for button in [*existing_list, *new_list]:
        normalized = _normalize_button_payload(button)
        if not normalized:
            continue
        marker = json.dumps(normalized, ensure_ascii=False, sort_keys=True, default=str)
        if marker in seen:
            continue
        seen.add(marker)
        merged.append(normalized)

    return merged


def _merge_extra_data_buttons(context: ToolContext, new_buttons: list[dict[str, Any]]) -> None:
    """Merge button payloads into extra_data without dropping other keys."""
    current_extra_data = context.state.get("extra_data", {})
    if not isinstance(current_extra_data, dict):
        current_extra_data = {}

    current_extra_data["buttons"] = _merge_buttons(current_extra_data.get("buttons", []), new_buttons)
    context.state["extra_data"] = current_extra_data


def _replace_buttons_by_name(
    original_buttons: list[dict[str, Any]],
    resolved_buttons: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    resolved_by_name: dict[str, dict[str, Any]] = {}
    for button in resolved_buttons:
        normalized = _normalize_button_payload(button)
        if not normalized:
            continue
        button_name = normalized.get("name")
        if isinstance(button_name, str) and button_name:
            resolved_by_name[button_name] = normalized

    final_buttons: list[dict[str, Any]] = []
    for button in original_buttons:
        normalized = _normalize_button_payload(button)
        if not normalized:
            continue
        button_name = normalized.get("name")
        if isinstance(button_name, str) and button_name in resolved_by_name:
            final_buttons.append(resolved_by_name[button_name])
        else:
            final_buttons.append(normalized)

    return final_buttons


def select_fields_to_ask(missing_fields: list[str], number_of_fields: int = 3) -> list[str]:
    """Random select max 3 fields to ask. School fields always go together."""

    # If <= number_of_fields fields missing, ask all
    if len(missing_fields) <= number_of_fields:
        return missing_fields

    # Identify school fields
    school_fields = [f for f in missing_fields if "THPT" in f or "Tỉnh/Thành phố" in f]

    if len(school_fields) == 2:
        remaining_fields = [f for f in missing_fields if f not in school_fields]

        # If enough slots, add more fields
        num_remaining = number_of_fields - 2
        if num_remaining > 0:
            selected_others = random.sample(remaining_fields, min(num_remaining, len(remaining_fields)))
            return school_fields + selected_others
        return school_fields

    # Case 2: Normal random selection (no pair constraint)
    return random.sample(missing_fields, number_of_fields)


def check_missing_fields_user_profile(state: dict[str, Any]) -> list[Any]:
    """
    Check for missing fields in user profile from the state.

    Args:
        state: The current tool context state.

    Returns:
        A comma-separated string of missing field names in Vietnamese.
    """
    role = state.get("user_state", {}).get("role", None)
    field_mapping = {
        "student_name": "Họ tên của con" if role == "parent" else "Họ tên",
        "student_phone": "Số điện thoại",
        "student_email": "Email",
        "student_high_school": "Trường THPT của con" if role == "parent" else "Trường THPT",
        "student_high_school_province": "Địa chỉ(Tỉnh/Thành phố) của trường THPT",
    }
    invalid_values = {
        "null",
        "none",
        "n/a",
        "undefined",
        "-",
    }
    missing_fields = []

    if not isinstance(state, dict):
        missing_fields.append(field_mapping.values())
        return missing_fields

    for field_key, field_label in field_mapping.items():
        value = state.get(field_key, None)

        # 1. Not exist
        if value is None:
            missing_fields.append(field_label)
            continue

        # 2. Not string
        if not isinstance(value, str):
            missing_fields.append(field_label)
            continue

        # 3. String is empty after strip
        cleaned = value.strip()
        if not cleaned:
            missing_fields.append(field_label)
            continue

        # 4. Invalid value
        if cleaned.lower() in invalid_values:
            missing_fields.append(field_label)
            continue
    logger.warning(f"Missing fields: {missing_fields}")
    return missing_fields


def get_student_information_to_fill_form(state: dict[str, Any]) -> dict[str, Any] | None:
    """
    Get the user profile from the state.

    Args:
        state: The current tool context state.
    Returns:
        The user profile as a dictionary, or None if not found.
    """
    information_to_fill_form = {
        "student_name": state.get("student_name", None),
        "student_phone": state.get("student_phone", None),
        "student_email": state.get("student_email", None),
        "student_high_school_province": state.get("student_high_school_province", None),
        "student_high_school": state.get("student_high_school", None),
        "major": state.get("user_state", {}).get("major", None),
        "admission_methods": state.get("student_admission_methods", None)
        or state.get("user_state", {}).get("student_profile", {}).get("admission_methods", None),
    }
    logger.info(f"Student information: {information_to_fill_form}")
    return information_to_fill_form


def _join_mapping_codes(items: list[dict[str, str]] | None) -> str | None:
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
    return ",".join(codes) if codes else None


def get_deadline_paid_tuition(state: dict[str, Any]) -> str:
    """
    Get the deadline for paid tuition from the state.

    Args:
        state: The current tool context state.

    Returns:
        The deadline for paid tuition in Vietnamese.
    """
    return ""


def get_random_major(context: ToolContext) -> list[dict[str, Any]]:
    """
    Get 3 random majors from Neo4j.
    context

    Returns:
        List of dictionaries containing name, description, and career_opportunities of the majors.
    """
    try:
        neo4j_service = Neo4jService()
        query = """
        MATCH (n:Major|Specialization) 
        RETURN n.name as name
        """
        result = neo4j_service.cypher_query(query)
        if result.get("status") == "success":
            response = sanitize_neo4j_types(result.get("results", []))
            logger.info(f"get_random_major response: {response}")
            return response
        else:
            logger.error(f"Failed to get random majors: {result.get('message')}")
            return []
    except Exception as e:
        logger.error(f"Error in get_random_major: {e}")
        traceback.print_exc()
        return []


def confirm_school_address(context: ToolContext) -> None:
    """
    Confirm school address by checking state keys and searching for matching schools.
    Stores results directly into context.state.

    This function checks the existence and values of keys in state:
    1. selected_school_option - User selected from a list of options
    2. student_high_school
    3. student_high_school_province
    4. student_high_school_address

    Logic:
    - If selected_school_option is set AND playbook_action_results has data → use selected ID
    - If key 1 or key 2 does not exist/has no value → do nothing
    - If key 1 and 2 exist with values, but key 3 does not → search with keys 1 and 2
    - If all 3 keys exist with values → search with all 3 keys:
        - If results not empty: has_crm_school_id = True, crm_school_id = first result's id
        - If results empty: has_crm_school_id = False

    State updates (only if results is not None):
    - has_crm_school_id: bool - True only when all 3 keys valid AND search has results
    - crm_school_id: str | None - ID of first result (only when has_crm_school_id is True)
    - playbook_action_results: List - List of search results

    Args:
        context: The tool context containing state.
    """
    state = context.state.to_dict()
    invalid_values = {"null", "none", "n/a", "undefined", "-", ""}

    def has_valid_value(value: Any) -> bool:
        """Check if a value exists and is valid (non-empty, not null-like)."""
        if value is None:
            return False
        if not isinstance(value, str):
            return False
        cleaned = value.strip()
        if not cleaned or cleaned.lower() in invalid_values:
            return False
        return True

    def reset_confirmation_state() -> None:
        context.state["has_crm_school_id"] = False
        context.state["crm_school_id"] = None
        context.state["playbook_action_results"] = None
        context.state["is_pending_school_confirmation"] = False
        context.state["pending_school_confirmation_instruction"] = None
        context.state["pending_school_confirmation_answer_guidance"] = None

    def parse_playbook_results(raw_results: Any) -> list[dict[str, Any]]:
        if isinstance(raw_results, list):
            return raw_results
        if not isinstance(raw_results, str):
            return []
        try:
            parsed = ast.literal_eval(raw_results)
        except (ValueError, SyntaxError):
            logger.error(f"Failed to parse playbook_action_results: {raw_results}")
            return []
        return parsed if isinstance(parsed, list) else []

    def set_province_location_if_missing(user_state_data: dict[str, Any], province_value: str | None, message: str) -> None:
        current_province_location = state.get("student_province_location")
        if has_valid_value(current_province_location) or not province_value:
            return
        normalized_province = normalize_province(province_value)
        context.state["student_province_location"] = normalized_province
        user_state_data.setdefault("student_profile", {})["province_location"] = normalized_province
        context.state["user_state"] = user_state_data
        logger.info(message, province_value, normalized_province)

    try:
        # Check for selected_school_option first
        user_state = state.get("user_state", {})
        selected_option = user_state.get("selected_school_option")
        playbook_results_raw = state.get("playbook_action_results")
        has_crm_school_id = state.get("has_crm_school_id", False)
        is_pending_school_confirmation = state.get("is_pending_school_confirmation", False)
        logger.info(f"has_crm_school_id before check: {has_crm_school_id}")
        logger.info(f"is_pending_school_confirmation: {is_pending_school_confirmation}")

        # Already confirmed school, skip
        if has_crm_school_id:
            context.state["is_pending_school_confirmation"] = False
            context.state["pending_school_confirmation_instruction"] = None
            context.state["pending_school_confirmation_answer_guidance"] = None
            context.state["playbook_action_instruction"] = None
            return

        # Note: Removed is_pending_school_confirmation early return lock.
        # This allows users to change their school before confirming.

        if selected_option is not None and playbook_results_raw:
            playbook_results = parse_playbook_results(playbook_results_raw)
            if isinstance(playbook_results, list) and len(playbook_results) >= selected_option:
                selected_school = playbook_results[selected_option - 1]
                selected_id = selected_school.get("id")

                if selected_id:
                    logger.info(
                        f"confirm_school_address: User selected option {selected_option}, using ID: {selected_id}"
                    )
                    context.state["has_crm_school_id"] = True
                    context.state["crm_school_id"] = [selected_id]
                    context.state["playbook_action_results"] = None
                    context.state["playbook_action_instruction"] = None
                    context.state["is_pending_school_confirmation"] = False
                    context.state["pending_school_confirmation_instruction"] = None
                    context.state["pending_school_confirmation_answer_guidance"] = None

                    set_province_location_if_missing(
                        user_state_data=user_state,
                        province_value=selected_school.get("province"),
                        message=(
                            "confirm_school_address: Derived province_location from selected school: "
                            "%s -> normalized: %s"
                        ),
                    )

                    logger.info(f"has_crm_school_id after check with selection: {context.state['has_crm_school_id']}")
                    return
            else:
                logger.info(
                    f"confirm_school_address: Invalid selection {selected_option}, results count: {len(playbook_results) if isinstance(playbook_results, list) else 0}"
                )

        # Get values from state
        student_high_school = state.get("student_high_school")
        student_high_school_province = state.get("student_high_school_province")
        student_high_school_address = state.get("student_high_school_address")

        province_candidates = []
        if has_valid_value(student_high_school_province):
            raw_p = str(student_high_school_province).strip()
            province_candidates.append(raw_p)
            from utils.province_normalizer import get_province_34

            p34 = get_province_34(raw_p)
            if p34 and p34 != raw_p:
                province_candidates.append(p34)
            logger.info(f"confirm_school_address: province candidates: {province_candidates}")

        if not has_valid_value(student_high_school) or not has_valid_value(student_high_school_province):
            logger.warning("confirm_school_address: Missing high_school or high_school_province, skipping")
            return

        if has_valid_value(student_high_school_address):
            logger.info(
                f"confirm_school_address: Searching with all 3 keys - school: {student_high_school}, province: {student_high_school_province}, address: {student_high_school_address}"
            )
            results = []
            for sp in province_candidates:
                results = search_school_hybrid(
                    high_school=str(student_high_school).strip(),
                    high_school_province=sp,
                    high_school_address=str(student_high_school_address).strip(),
                )
                if results:
                    logger.info(f"confirm_school_address: Found with province='{sp}'")
                    break

            if results and len(results) > 0:
                crm_school_id = results[0].get("id")
                logger.info(f"confirm_school_address: Found school with crm_school_id: {crm_school_id}")
                context.state["has_crm_school_id"] = True
                context.state["crm_school_id"] = [crm_school_id]
                context.state["playbook_action_results"] = None
                context.state["playbook_action_instruction"] = None
                context.state["is_pending_school_confirmation"] = False
                context.state["pending_school_confirmation_instruction"] = None
                context.state["pending_school_confirmation_answer_guidance"] = None

                set_province_location_if_missing(
                    user_state_data=user_state,
                    province_value=student_high_school_province,
                    message=(
                        "confirm_school_address: Derived province_location from school province: "
                        "%s -> normalized: %s"
                    ),
                )
                return

            logger.info("confirm_school_address: No matching schools found with 3 keys")
            reset_confirmation_state()
            return

        logger.info(
            f"confirm_school_address: Searching with 2 keys - school: {student_high_school}, candidates: {province_candidates}"
        )
        results = []
        for sp in province_candidates:
            results = search_school_hybrid(high_school=str(student_high_school).strip(), high_school_province=sp)
            if results:
                logger.info(f"confirm_school_address: Found with province='{sp}'")
                break
        if results and len(results) > 0:
            context.state["has_crm_school_id"] = False
            context.state["crm_school_id"] = None
            context.state["playbook_action_results"] = results
            context.state["is_pending_school_confirmation"] = True
            if len(results) == 1:
                school_score = results[0].get("score")
                if school_score > 0.9:
                    context.state["has_crm_school_id"] = True
                    context.state["crm_school_id"] = [results[0].get("id")]
                    context.state["playbook_action_results"] = None
                    context.state["is_pending_school_confirmation"] = False
                    context.state["pending_school_confirmation_instruction"] = False
                    logger.info(
                        f"Auto submitting school {results[0].get('school_name')} with address {results[0].get('address')} in province {results[0].get('province')}, score: {school_score}")
                else:
                    logger.info("confirm_school_address: Found 1 school with 2 keys, showing confirm button")
                    _merge_extra_data_buttons(context, [{"name": "Xác nhận", "link": None}])
            logger.info(
                f"confirm_school_address: Set is_pending_school_confirmation=True, results count: {len(results)}"
            )
            return

        logger.info("confirm_school_address: No matching schools found with 3 keys")
        reset_confirmation_state()
    except Exception:
        traceback.print_exc()
        reset_confirmation_state()
        context.state["playbook_action_results"] = ""


class AnswerHintTemplateParam(StrEnum):
    """Enumeration for answer hint template parameters."""

    USER_PRONOUN = "user_pronoun"
    SELF_PRONOUN = "self_pronoun"


class ActionHintTemplateParam(StrEnum):
    """Enumeration for action hint template parameters."""

    PERSONALITY_PROFILE = "personality_profile"
    MAJOR = "major"
    USER_PRONOUN = "user_pronoun"
    SELF_PRONOUN = "self_pronoun"
    LATEST_USER_MESSAGES = "latest_user_messages"
    CONVERSATION_TURN = "conversation_turn"
    POTENTIAL_MAJORS = "potential_majors"
    MAJOR_INTEREST_SCORES = "major_interest_scores"


def _build_playbook_text_func_registry(context: ToolContext | Any) -> dict[str, Callable[[], str | None]]:
    context_state = context.state

    def handle_check_missing_fields() -> str | None:
        missing_fields = check_missing_fields_user_profile(context_state.to_dict())

        if not missing_fields:
            logger.warning("No missing fields  skip answer hint")
            return None

        fields_to_ask = select_fields_to_ask(missing_fields, number_of_fields=3)
        return ", ".join(fields_to_ask) if fields_to_ask else ""

    def handle_get_deadline_paid_tuition() -> str:
        return str(get_deadline_paid_tuition(context_state.to_dict()))

    def handle_get_student_information() -> str:
        return str(get_student_information_to_fill_form(context_state.to_dict()))

    def handle_get_random_major() -> str:
        return str(get_random_major(context=context))

    def ask_all_information() -> str | None:
        state_dict = context_state.to_dict()
        missing_fields = check_missing_fields_user_profile(state_dict)
        has_major = bool(state_dict.get("user_state", {}).get("major"))
        if not has_major:
            missing_fields.append("Ngành học hoặc lĩnh vực đang quan tâm")
        if not missing_fields:
            return "Không cần thu thập thông tin nữa. Vì đã có đủ thông tin rồi"
        return ", ".join(missing_fields)

    def recommend_majors_for_personalization() -> str:
        personalization = context_state.get("user_state", {}).get("personalization", [])
        recommend_majors_list = search_career_recommendation_candidates(original_query="", keywords=personalization)
        recommend_majors = [major.get("major") for major in recommend_majors_list]
        return "\n".join(recommend_majors)

    return {
        "check_missing_fields_user_profile": handle_check_missing_fields,
        "get_deadline_paid_tuition": handle_get_deadline_paid_tuition,
        "get_student_information_to_fill_form": handle_get_student_information,
        "get_random_major": handle_get_random_major,
        "ask_all_information": ask_all_information,
        "search_career_recommendation_candidates": recommend_majors_for_personalization,
    }


def resolve_playbook_text_hint(hint: str, context: ToolContext | Any) -> str | None:
    """
    Resolve playbook text by substituting template parameters and executing func(...).
    Returns None when a required func(...) resolves to None.
    """

    context_state = context.state
    params = {param.value: context_state.get(param.value) for param in AnswerHintTemplateParam}

    try:
        processed_hint = hint.format(**params)
    except (KeyError, ValueError) as e:
        logger.error(f"Hint format error: {e}. Hint: {hint}")
        processed_hint = hint

    func_registry = _build_playbook_text_func_registry(context)
    func_pattern = re.compile(r"func\(([\w_]+)\)")
    matches = func_pattern.findall(processed_hint)

    for func_name in matches:
        handler = func_registry.get(func_name)
        if not handler:
            logger.warning(f"Unknown func({func_name}) in hint")
            continue

        result = handler()
        if result is None and func_name != "ask_all_information":
            logger.info(f"func({func_name}) returned None  text hint skipped")
            return None

        replacement = result if isinstance(result, str) else ""
        processed_hint = processed_hint.replace(f"func({func_name})", replacement)

    return processed_hint


class PlaybookDMNHintProcessor:
    """Processor for fetching and handling DMN hints."""

    def __init__(self):
        """
        Initialize the DMNHintProcessor.

        Args:
            api_endpoint: The URL of the DMN hint service.
        """
        self.kogito_url = settings.kogito_endpoint_addressing_rule
        self.api_endpoint = f"{self.kogito_url}/playbook"

    def process_hints(self, context: ToolContext) -> dict[Any, Any] | tuple[Any, Any] | None:
        """
        Main entry point to process hints based on tool context.

        Args:
            context: The tool context containing state.

        Returns:
            The raw response from the DMN API (or empty dict on failure),
            augmented with processed hint actions.
        """
        # Check if pending school confirmation - preserve existing data
        is_pending_school_confirmation = context.state.get("is_pending_school_confirmation", False)
        context.state.get("playbook_action_results")

        # Reset playbook states immediately before processing new hints
        # so old data (e.g. high schools list from a previous stage) doesn't persist
        if not is_pending_school_confirmation:
            context.state["playbook_action_results"] = None
            context.state["playbook_action_instruction"] = None
        context.state["playbook_answer_guidance"] = None
        context.state["playbook_target"] = None

        # 1. Extract context data
        payload = self._extract_context_data(context)

        # 2. Call DMN API
        response_data = self._call_dmn_api(payload)
        if not response_data:
            return {}

        # 3. Process outputs
        output = response_data.get("output", [])
        logger.info(f"Playbook DMN output: {output}")
        processed_results = {}
        if output:
            output = output[0]
            answer_hint = output.get("answer_hint")
            if answer_hint:
                self._handle_answer_hint(answer_hint, context)
                processed_results["playbook_answer_guidance"] = context.state.get("playbook_answer_guidance")

            cta_hints = output.get("cta_hint", [])
            if cta_hints:
                self._handle_cta_hint(cta_hints, context)
                processed_results["cta_hint"] = context.state.get("cta_hint")
            else:
                # No cta_hints from Playbook DMN, set buttons to empty list
                logger.warning("No cta_hints from Playbook DMN, setting buttons to empty list")
                current_extra_data = context.state.get("extra_data", {})
                current_extra_data["buttons"] = []
                # Explicitly re-assign extra_data to ensure persistence
                context.state["extra_data"] = current_extra_data

            action_hints = output.get("action_hint", [])
            if action_hints:
                self._handle_action_hint(action_hints, context)
                processed_results["playbook_action_results"] = context.state.get("playbook_action_results")
                processed_results["playbook_guideline_action_instruction"] = context.state.get("playbook_guideline_action_instruction")

            target = output.get("target")
            if target is not None:
                context.state["playbook_target"] = target
                processed_results["playbook_target"] = target

            # Handle pending school confirmation
            # current_is_pending = context.state.get("is_pending_school_confirmation", False)
            #
            # if current_is_pending and existing_playbook_action_results:
            #     # Skip restore if school already confirmed
            #     if context.state.get("has_crm_school_id", False):
            #         logger.error("Pending school confirmation - skipping restore, school already confirmed")
            #     else:
            #         # Check if we already have saved data (not first time)
            #         saved_instruction = context.state.get("pending_school_confirmation_instruction")
            #         saved_answer_guidance = context.state.get("pending_school_confirmation_answer_guidance")
            #
            #         if saved_instruction:
            #             # Not first time - restore all saved data
            #             logger.error("Pending school confirmation (repeat) - restoring all data")
            #             context.state["playbook_action_results"] = existing_playbook_action_results
            #             context.state["playbook_action_instruction"] = saved_instruction
            #             if saved_answer_guidance:
            #                 context.state["playbook_answer_guidance"] = saved_answer_guidance
            #             logger.error(
            #                 f"Restored: playbook_action_results, instruction: {saved_instruction}, answer_guidance: {saved_answer_guidance}"
            #             )
            # else:
            #     # First time - save current data for future use
            #     current_instruction = context.state.get("playbook_action_instruction")
            #     current_answer_guidance = context.state.get("playbook_answer_guidance")
            #
            #     if current_instruction:
            #         context.state["pending_school_confirmation_instruction"] = current_instruction
            #         logger.error(f"First time pending - saved instruction: {current_instruction}")
            #     if current_answer_guidance:
            #         context.state["pending_school_confirmation_answer_guidance"] = current_answer_guidance
            #         logger.error(f"First time pending - saved answer_guidance: {current_answer_guidance}")
            #
            #     # Restore playbook_action_results (school list) that was set by confirm_school_address
            #     context.state["playbook_action_results"] = existing_playbook_action_results
            #     logger.error(f"First time pending - restored playbook_action_results")

            rule_id = output.get("rule_id")
            skip_count = output.get("skip_count")
            return rule_id, skip_count
        return None

    def _extract_context_data(self, context: ToolContext) -> dict[str, Any]:
        """
        Extracts relevant fields from ToolContext state.

        Args:
            context: The tool context.

        Returns:
            Dictionary matching the DMN API input schema.
        """
        state = context.state.to_dict()
        state["user_state"].get("is_requested_advise", False)
        is_requested_advise_major = state["user_state"].get("is_requested_advise_major", False)
        is_confirm_information_to_fill_form = state["user_state"].get("is_confirm_information_to_fill_form", False)
        current_major = state["user_state"].get("interested_majors", [])
        if not current_major:
            current_major = state["user_state"].get("majors", [])
        is_spam = state["user_state"].get("is_spam")
        intent_flags = extract_turn_intent_flags(state.get("user_state", {}))
        intent_override = state.get("playbook_turn_intent_override")
        has_potential_majors = bool(state["user_state"].get("potential_majors"))
        if not has_potential_majors:
            has_potential_majors = bool(state["user_state"].get("majors", []))
        has_personalization = bool(state["user_state"].get("personalization", []))
        if isinstance(intent_override, dict):
            for field in TURN_INTENT_FIELDS:
                if field in intent_override:
                    intent_flags[field] = bool(intent_override[field])
        check_missing_fields_user_profile(state)
        has_profile_info = state.get("has_profile_info", False)
        has_contact_info = _has_contact_info(state)
        logger.info(f"has_profile_info: {has_profile_info}")
        current_stage = state.get("playbook_stage_override")
        if current_stage is None:
            current_stage = state.get("current_stage", "")

        current_status = state.get("playbook_status_override")
        if current_status is None:
            current_status = state.get("current_status", "")

        data = {
            "current_stage": current_stage.lower() if isinstance(current_stage, str) else "",
            "current_status": current_status.lower() if isinstance(current_status, str) else "",
            "is_spam": is_spam if is_spam is not None else False,
            # "current_stage": "prospecting",
            # "current_status": "engaged",
            "has_role": bool(state["user_state"].get("role") is not None),
            # "has_topic": bool(state["user_state"].get("topic") is not None and state["user_state"].get("topic") != ""),
            "has_personal_info": has_profile_info if has_profile_info is not None else False,
            "has_contact_info": has_contact_info,
            "has_personalization": has_personalization,
            "has_potential_majors": has_potential_majors,
            "is_submitted": state.get("is_submitted", False),
            "is_verified_profile": state.get("is_verified_profile", False),
            "is_deposited": state.get("is_deposited", state.get("has_deposited", False)),
            "is_signed": state.get("is_signed", state.get("has_signed", False)),
            "has_major": bool(current_major),
            "is_uploaded_document": state.get("is_uploaded_document", False),
            "is_confirm_information_to_fill_form": is_confirm_information_to_fill_form
            if is_confirm_information_to_fill_form is not None
            else False,
            "is_paid": state.get("is_paid", False),
            "is_requested_advise_major": is_requested_advise_major if is_requested_advise_major is not None else False,
        }
        for field in TURN_INTENT_FIELDS:
            data[field] = bool(intent_flags.get(field))

        logger.info(f"Extracted DMN context data: {data}")
        return data

    def _call_dmn_api(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """
        Calls the external DMN service.

        Args:
            payload: Input data for the API.

        Returns:
            JSON response from API or None on failure.
        """
        try:
            logger.info(f"Calling DMN API at {self.api_endpoint}")
            response = requests.post(self.api_endpoint, json=payload, timeout=5)
            response.raise_for_status()
            logger.info(f"Playbook response {response.text}")
            return response.json()
        except requests.RequestException as e:
            traceback.print_exc()
            logger.error(f"Failed to call DMN API: {e}")
            return None

    def _handle_answer_hint(self, hint: str, context: ToolContext) -> None:
        """
        Process answer hint by substituting template parameters and executing func(...)
        Stores result in `context.state['playbook_answer_guidance']`
        """
        processed_hint = resolve_playbook_text_hint(hint, context)
        if processed_hint is None:
            context.state["playbook_answer_guidance"] = None
            return

        context.state["playbook_answer_guidance"] = processed_hint
        logger.info(f"Processed answer hint: {processed_hint}")

    def _handle_cta_hint(self, hints: list[dict], context: ToolContext) -> None:
        """
        Process Call-To-Action hints by MERGING buttons into extra_data.
        PlaybookDMNHintProcessor is the main source for CTA buttons, but
        action handlers may add their own confirmation buttons in the same turn.

        Args:
            hints: List of CTA hint dicts.
            context: The tool context to store the result.
        """
        original_buttons = [_normalize_button_payload(hint) for hint in hints]
        original_buttons = [button for button in original_buttons if button]
        new_buttons = []
        empty_query_params = "__empty"

        exists_func = any("func" in hint for hint in hints)
        if exists_func:
            logger.info("CTA hint contains func, processing special case")
            func_name = find_object_by_key(data=hints, key="func")
            logger.info(f"CTA hint func_name: {func_name}")

            if func_name and func_name.get("func") == "submit_enrollment_form":
                enrollment_data = get_student_information_to_fill_form(context.state.to_dict()) or {}

                # Get high school ID safely
                grade12_school = empty_query_params
                crm_school_id = context.state.get("crm_school_id")
                if enrollment_data.get("student_high_school") is not None:
                    if crm_school_id:
                        grade12_school = int(crm_school_id[0])

                # Get majors safely
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
                # majors_code = [list(major.keys()) for major in majors]
                # specialization_code = [major_code for ]
                logger.info(f"CTA majors_code: {majors_code}")
                logger.info(f"CTA specialization_code: {specialization_code}")
                admission_method_codes = _join_mapping_codes(enrollment_data.get("admission_methods"))

                form_data = ApplicationFormData(
                    full_name=enrollment_data.get("student_name"),
                    student_phone=enrollment_data.get("student_phone"),
                    email=enrollment_data.get("student_email"),
                    grade12_school=grade12_school,
                    grade12_province=enrollment_data.get("student_high_school_province"),
                    section_id=context.session.id,
                    platform=context.state.get("platform"),
                    majors=",".join(majors_code) if majors_code else empty_query_params,
                    specializations=",".join(specialization_code) if specialization_code else empty_query_params,
                    admission_methods=admission_method_codes or empty_query_params,
                )
                review_form_link = submit_application_form(form_data=form_data)
                logger.info(f"Generated review_form_link: {review_form_link}")
                # For submit_enrollment_form, create a single button object
                new_buttons = [
                    {
                        "name": func_name.get("name"),
                        "link": review_form_link,
                        "func": func_name.get("func"),
                    }
                ]
            else:
                # func exists but not submit_enrollment_form, use all hints as buttons
                new_buttons = hints
            logger.info(f"Processed cta_hint (func case): {new_buttons}")
        else:
            logger.info(f"CTA hint without func, setting buttons with hints: {hints}")
            new_buttons = hints
            logger.info(f"Processed cta_hint: {new_buttons}")

        final_buttons = _replace_buttons_by_name(original_buttons, new_buttons) if exists_func else original_buttons
        context.state["cta_hint"] = final_buttons
        _merge_extra_data_buttons(context, final_buttons)

    def _handle_action_hint(self, hints: list[dict], context: ToolContext) -> None:
        """
        Process action hints by parsing JSON, filtering for instructions,
        and substituting template parameters.
        Stores the result in `context.state['playbook_action_instruction']` for instructions
        and `context.state['playbook_action_results']` for execute results.

        Args:
            hints: List of action hint dicts.
            context: The tool context to retrieve params and store result.
        """
        processed_instructions = []
        guideline_instructions = []
        context_state = context.state

        # 1. Reuse param extraction logic for action hints
        params = {}
        for param_enum in ActionHintTemplateParam:
            param_key = param_enum.value
            value = context_state.get(param_key)
            # Default to empty string if missing
            params[param_key] = value if value is not None else ""

        # Map of supported functions for type="execute"
        execute_functions = {
            "confirm_school_address": confirm_school_address,
        }

        for data in hints:
            try:
                logger.info(f"data playbook action_hint: {data}")
                hint_type = data.get("type")

                if hint_type == "instruction":
                    prompt = data.get("prompt", "")
                    if not prompt:
                        continue
                    try:
                        logger.info("before build prompt")
                        processed_prompt = prompt.format(**params)
                    except (KeyError, ValueError) as e:
                        logger.error(f"Error formatting action hint: {e}. Prompt: {prompt}")
                        processed_prompt = prompt
                    logger.info(f"processed_prompt: {processed_prompt}")
                    processed_instructions.append(processed_prompt)
                    continue

                if hint_type == "execute":
                    func_name = data.get("func", "")
                    if not func_name or func_name not in execute_functions:
                        logger.warning(f"Unknown or missing function in execute hint: {func_name}")
                        continue
                    logger.info(f"Executing function: {func_name}")
                    try:
                        execute_functions[func_name](context)
                    except Exception as e:
                        logger.error(f"Error executing function {func_name}: {e}")
                    else:
                        logger.info(f"Successfully executed function: {func_name}")
                    continue

                if hint_type == "query":
                    handle_action_type_query(data, context, type_hint="playbook")
                if hint_type == "guideline":
                    prompt = data.get("prompt", "")
                    if not prompt:
                        continue
                    try:
                        logger.info("before build guideline prompt")
                        processed_prompt = prompt.format(**params)
                    except (KeyError, ValueError) as e:
                        logger.error(f"Error formatting action hint: {e}. Prompt: {prompt}")
                        processed_prompt = prompt
                    logger.info(f"processed guideline prompt: {processed_prompt}")
                    guideline_instructions.append(processed_prompt)
                    continue
            except Exception as e:
                logger.error(f"Error processing action hint: {data}, error: {e}")
                continue

        # Store instructions in state (separate from execute results)
        if processed_instructions:
            context.state["playbook_action_instruction"] = processed_instructions
            logger.info(f"Processed {len(processed_instructions)} action instruction hints.")
        if guideline_instructions:
            context.state["playbook_guideline_action_instruction"] = guideline_instructions
