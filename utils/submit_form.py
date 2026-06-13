import logging
import traceback
from typing import Any
from urllib.parse import urlencode

import requests
from utils.logging_config import log_event
from configs.config_service import get_settings
from schemas.submit_form import ApplicationFormData

logger = logging.getLogger(__name__)
settings = get_settings()


def _normalize_gender_for_crm(gender: str | None) -> str | None:
    if not isinstance(gender, str):
        return None

    normalized = gender.strip().casefold()
    mapping = {
        "male": "Male",
        "nam": "Male",
        "m": "Male",
        "female": "Female",
        "nu": "Female",
        "nữ": "Female",
        "f": "Female",
        "other": "Other",
        "khac": "Other",
        "khác": "Other",
    }
    return mapping.get(normalized)


def _split_csv(raw_value: str | None) -> list[str]:
    if not isinstance(raw_value, str):
        return []
    return [item.strip() for item in raw_value.split(",") if item.strip() and item.strip() != "__empty"]


def _build_preferences_payload(form_data: ApplicationFormData) -> list[dict[str, str | None]]:
    majors = _split_csv(form_data.majors)
    specializations = _split_csv(form_data.specializations)

    preferences: list[dict[str, str | None]] = []
    max_len = max(len(majors), len(specializations))

    for index in range(max_len):
        major_code = majors[index] if index < len(majors) else None
        specialization_code = specializations[index] if index < len(specializations) else None

        if not major_code and specialization_code and "-" in specialization_code:
            major_code = specialization_code.split("-", 1)[0]

        if not major_code and not specialization_code:
            continue

        preferences.append(
            {
                "major": major_code,
                "specialization": specialization_code,
            }
        )

    return preferences


def build_crm_application_payload(form_data: ApplicationFormData) -> dict:
    preferences = _build_preferences_payload(form_data)
    payload = {
        "conversation_id": form_data.section_id,
        "full_name": form_data.full_name,
        "national_id": form_data.national_id,
        "parent_phone": form_data.parent_phone,
        "gender": _normalize_gender_for_crm(form_data.gender),
        "email": form_data.email,
        "date_of_birth": form_data.birth_date.isoformat() if form_data.birth_date else None,
        "student_phone": form_data.student_phone,
        "permanent_street_address": form_data.permanent_street,
        "permanent_ward": form_data.permanent_ward,
        "permanent_province": form_data.permanent_province,
        "grade_12_province": form_data.grade12_province,
        "grade_12_class": form_data.grade12_class,
        "grade_12_school": str(form_data.grade12_school) if form_data.grade12_school not in (None, "") else None,
        "graduation_year": form_data.graduation_year,
        "receiving_province": form_data.receiving_province,
        "receiving_ward": form_data.receiving_ward,
        "receiving_street_address": form_data.receiving_street,
        "admission_methods": form_data.admission_methods,
        "preferences": preferences or None,
    }
    return payload


def submit_application_form(form_data: ApplicationFormData, base_url: str = settings.application_form_base_url) -> str:
    # Get query parameters from the form data
    params = form_data.to_query_params()

    # Build full URL
    full_url = f"{base_url}?{urlencode(params)}" if params else base_url

    return full_url


def auto_submit_form(form_data: ApplicationFormData, base_url: str = settings.crm_submit_form_endpoint) -> (
        None | bool |tuple[bool | Any, Any]):
    try:
        payload = build_crm_application_payload(form_data)
        print(f"CRM auto submit form: {payload}")
        if not base_url:
            logger.warning("CRM auto submit skipped because crm_submit_form_endpoint is empty")
            return False

        if base_url.startswith("http://") or base_url.startswith("https://"):
            full_url = base_url
        else:
            full_url = f"{settings.crm_host.rstrip('/')}/{base_url.lstrip('/')}"

        response = requests.post(
            full_url,
            json=payload,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"token {settings.crm_api_key}",
            },
            timeout=settings.crm_timeout_seconds,
        )
        status_code = response.status_code
        text = response.text
        print(f"CRM auto-submit response: {status_code}")
        print(f"CRM auto-submit response: {text}")
        return status_code == 200, text
    except Exception as e:
        traceback.print_exc()
