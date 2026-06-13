import json
import traceback

import requests

from configs.config_service import get_settings
from schemas.stage_variable import UserState
from utils.logging_config import get_logger

logger = get_logger(__name__)
settings = get_settings()


def ensure_user_state(state_data) -> UserState:
    """
    Ensure state_data is a UserState object.
    Handles both dict (from DB) and UserState (in-memory) inputs.

    Args:
            state_data: Either a UserState object or dict

    Returns:
            UserState object
    """
    try:
        logger.debug(f"[ensure_user_state] State: {state_data}")
        if state_data is None:
            return UserState()

        if isinstance(state_data, UserState):
            return state_data

        if isinstance(state_data, dict):
            return UserState(**state_data)

        logger.warning(f" Unexpected state type: {type(state_data)}, returning empty UserState")
        return UserState()
    except Exception:
        traceback.print_exc()
        return UserState()


def get_cta_hint(user_state: UserState | None, stage: int | None = 1) -> dict:

    try:
        user_state = user_state or UserState()

        # Extract student_profile
        # student_profile_val = None
        # if user_state.student_profile and user_state.student_profile.full_name and user_state.student_profile.phone and user_state.student_profile.email and user_state.student_profile.high_school:
        # 	student_profile_val = user_state.student_profile.model_dump()

        student_profile_val = None

        profile = user_state.student_profile
        # Only check required fields (exclude passively extracted fields: gender, province_location)
        required_profile_fields = {
            "full_name",
            "phone",
            "email",
            "high_school",
            "high_school_province",
            "high_school_address",
        }
        if profile and all(getattr(profile, field) is not None for field in required_profile_fields):
            student_profile_val = profile.model_dump()

        #  Build payload - now safe to access .role, .topic, .major
        payload = {
            "stage_input": stage,
            "role_input": user_state.role if user_state.role else None,
            "topic_input": user_state.topic if user_state.topic else None,
            "major_input": user_state.major if user_state.major else None,
            "student_profile_input": student_profile_val,
        }

        logger.info(f"[get_cta_hint] Payload: {json.dumps(payload, indent=2, ensure_ascii=False)}")

        response = requests.post(
            url=f"{settings.kogito_endpoint_addressing_rule}/cta_hint",
            headers={"accept": "application/json", "Content-Type": "application/json"},
            json=payload,
            timeout=5,
        )
        response.raise_for_status()
        logger.debug("[get_cta_hint] response %s", response.text)
        output = response.json().get("Output", {})
        logger.debug("[get_cta_hint] Output: %s", output)

        return output

    except Exception as e:
        traceback.print_exc()
        logger.error(f"CTA hint API failed: {e}")
        err_dict = {"type": "", "value": None}
        logger.error("[get_cta_hint] error %s", err_dict)
        return {}
