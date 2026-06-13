import logging
import traceback
from typing import Any

import requests
from google.adk.tools import ToolContext

from configs.config_service import get_settings
from tools.playbook_hint_processing import check_missing_fields_user_profile
from tools.stage_progression import TURN_INTENT_FIELDS, extract_turn_intent_flags

logger = logging.getLogger(__name__)
settings = get_settings()


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


class DMNStageDetectorProcessor:
    """Processor for fetching and handling DMN hints."""

    def __init__(self):
        """
        Initialize the DMNHintProcessor.

        Args:
            api_endpoint: The URL of the DMN hint service.
        """
        self.kogito_url = settings.kogito_endpoint_addressing_rule
        self.api_endpoint = f"{self.kogito_url}/stage_detector"

    def stage_detector(
        self,
        context: ToolContext,
        intent_override: dict[str, bool] | None = None,
    ) -> dict[str, Any]:
        """
        Main entry point to process hints based on tool context.

        Args:
            context: The tool context containing state.

        Returns:
            The raw response from the DMN API (or empty dict on failure),
            augmented with processed hint actions.
        """
        # 1. Extract context data
        payload = self._extract_state_context_data(context, intent_override=intent_override)

        # 2. Call DMN API
        response_data = self._call_dmn_state_detector_api(payload)
        if not response_data:
            return {
                "next_status": context.state.get("current_status", None),
                "next_stage": context.state.get("current_stage", None),
            }

        # 3. Process outputs
        output = response_data.get("output", {})
        if output:
            return {
                "next_status": output.get("next_status", None),
                "next_stage": output.get("next_stage", None),
            }
        return {"next_status": None, "next_stage": None}

    def _extract_state_context_data(
        self,
        context: ToolContext,
        intent_override: dict[str, bool] | None = None,
    ) -> dict[str, Any]:
        """
        Extracts relevant fields from ToolContext state.

        Args:
            context: The tool context.

        Returns:
            Dictionary matching the DMN API input schema.
        """
        state = context.state.to_dict()
        personalization = state["user_state"].get("personalization", [])
        has_personalization = bool(personalization) if personalization else False
        is_requested_advise_major = state["user_state"].get("is_requested_advise_major", None)
        is_requested_advisor = state["user_state"].get("is_requested_advisor", False)
        topic_input = list(state["user_state"].get("topic") or [])
        current_major = state["user_state"].get("interested_majors", [])
        if not current_major:
            current_major = state["user_state"].get("majors", [])
        has_potential_majors = bool(state["user_state"].get("potential_majors", []))
        if not has_potential_majors:
            has_potential_majors = bool(state["user_state"].get("majors", []))
        intent_flags = extract_turn_intent_flags(state.get("user_state", {}))
        if intent_override:
            intent_flags.update(
                {field: bool(value) for field, value in intent_override.items() if field in TURN_INTENT_FIELDS}
            )

        missing_fields = check_missing_fields_user_profile(state)
        has_profile_info = not missing_fields
        has_contact_info = _has_contact_info(state)
        data = {
            "has_role": bool(state["user_state"].get("role", "")),
            "topic": topic_input if topic_input is not None else None,
            "has_profile_info": has_profile_info,
            "has_contact_info": has_contact_info,
            "has_potential_majors": has_potential_majors,
            # "is_requested_advise_major": is_requested_advise_major if is_requested_advise_major is not None else None,
            "has_major": bool(current_major),
            "is_submitted": state.get("is_submitted", False),
            "is_requested_advisor": is_requested_advisor if is_requested_advisor is not None else False,
            "is_uploaded_document": state.get("is_uploaded_document", False),
            "is_verified_profile": state.get("is_verified_profile", False),
            "is_signed": state.get("is_signed", False),
            "is_deposited": state.get("is_deposited", False),
            # "has_personalization": has_personalization,
            "is_paid": state.get("is_paid", False),
        }
        for field in TURN_INTENT_FIELDS:
            data[field] = bool(intent_flags.get(field))

        context.state["has_profile_info"] = has_profile_info
        context.state["has_contact_info"] = has_contact_info

        logger.info(f"Extracted DMN context data: {data}")
        return data

    def _call_dmn_state_detector_api(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """
        Calls the external DMN service.

        Args:
            payload: Input data for the API.

        Returns:
            JSON response from API or None on failure.
        """
        try:
            logger.info(f"Calling DMN API at {self.api_endpoint}")
            logger.debug(f"payload _call_dmn_state_detector_api: {payload}")
            response = requests.post(self.api_endpoint, json=payload, timeout=5)
            response.raise_for_status()
            logger.info(f"Result from DMN API received {response.text}")
            return response.json()
        except requests.RequestException as e:
            traceback.print_exc()
            logger.error(f"Failed to call DMN API: {e}")
            return None
