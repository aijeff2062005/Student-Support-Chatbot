import json
import logging
import re
import traceback
import unicodedata
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from typing import Any

import litellm

from configs.config_service import get_settings as _get_settings
from configs.llm_client import get_litellm_config
from dbs.milvus_helper import search_entity_by_name
from prompts.data_collector.stage_extractor import SYSTEM_PROMPT
from schemas.stage_variable import PersonalInfo, UserRole, UserState
from tools.QA.services.neo4j_service import Neo4jService
from tools.stage_progression import TURN_INTENT_FIELDS
from utils.province_normalizer import get_province_34, normalize_province

logger = logging.getLogger(__name__)

neo4j_service = Neo4jService()


def normalize_for_json(obj):
    # 0. Primitive
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj

    if isinstance(obj, Enum):
        return obj.value

    # 2. Dict
    if isinstance(obj, dict):
        return {k: normalize_for_json(v) for k, v in obj.items()}

    # 3. List / Tuple / Set
    if isinstance(obj, (list, tuple, set)):
        return [normalize_for_json(v) for v in obj]

    # 4. Dataclass
    if is_dataclass(obj):
        return normalize_for_json(asdict(obj))

    if hasattr(obj, "__dict__"):
        return {k: normalize_for_json(v) for k, v in obj.__dict__.items() if not k.startswith("_")}

    # 6. Fallback
    return str(obj)


def _merge_personalization(current_list: list, new_value) -> list:
    """Merge and deduplicate personalization lists."""
    result = current_list.copy()

    # Handle different input types
    if isinstance(new_value, str):
        new_items = [new_value]
    elif isinstance(new_value, list):
        new_items = new_value
    else:
        new_items = []

    # Add new items if not already present (case-insensitive deduplication)
    current_lower = [item.lower() for item in result]
    for item in new_items:
        if item and item.lower() not in current_lower:
            result.append(item)
            current_lower.append(item.lower())

    return result


def _coerce_bool(value: Any) -> bool:
    """Best-effort conversion for LLM-emitted boolean fields."""
    if isinstance(value, bool):
        return value

    if value is None:
        return False

    if isinstance(value, str):
        normalized_value = value.strip().lower()
        if normalized_value in {"true", "1", "yes"}:
            return True
        if normalized_value in {"false", "0", "no", "", "null", "none"}:
            return False

    if isinstance(value, (int, float)):
        return bool(value)

    return False


def _strip_accents(text: str) -> str:
    normalized = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")


def _fold_text(value: str | None) -> str:
    if not isinstance(value, str):
        return ""
    text = _strip_accents(value).replace("đ", "d").replace("Đ", "D").lower()
    return " ".join(text.split())


def _user_input_contains_value(user_input: str, value: str | None) -> bool:
    folded_user_input = _fold_text(user_input)
    folded_value = _fold_text(value)
    if not folded_user_input or not folded_value:
        return False

    return folded_value in folded_user_input


def _province_to_current_value(province_value: str | None) -> str | None:
    if not isinstance(province_value, str) or not province_value.strip():
        return None

    normalized_province = normalize_province(province_value)
    if not isinstance(normalized_province, str) or not normalized_province.strip():
        return None

    return get_province_34(normalized_province) or normalized_province


def _is_copied_current_province(
    raw_province_value: str | None,
    current_province_value: str | None,
    user_input: str,
) -> bool:
    if not raw_province_value or not current_province_value:
        return False

    if _user_input_contains_value(user_input, raw_province_value):
        return False

    raw_current_value = _province_to_current_value(raw_province_value)
    return bool(raw_current_value and _fold_text(raw_current_value) == _fold_text(current_province_value))


def _clean_major_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""

    text = value.strip().strip("\"'")
    text = re.sub(
        r"^(ngành học bạn quan tâm|nganh hoc ban quan tam|major(?:\(s\))?|majors?)\s*:\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"^[\-\*\u2022\d\.\)\(]+\s*", "", text)
    text = re.sub(r"\s+", " ", text).strip(" ,;")
    return text


ADMISSION_METHOD_CODE_TO_NAME = {
    "100": "Xét tuyển theo kết quả điểm thi tốt nghiệp THPT năm 2026",
    "204": "Xét tuyển theo Học bạ - Điểm trung bình chung kết quả học tập của 06 học kỳ (lớp 10,11,12) các môn xét tuyển theo tổ hợp 2026",
    "402": "Xét tuyển theo Điểm đánh giá năng lực do ĐHQG Tp.HCM, ĐHQG Hà Nội tổ chức 2026",
    "214": "Xét tuyển theo Điểm thi tốt nghiệp THPT của các môn xét tuyển + điểm xét tốt nghiệp THPT năm 2026",
    "301": "Xét tuyển thẳng theo quy định của Bộ GD&ĐT 2026",
}

ADMISSION_METHOD_NAME_TO_CODE = {
    method_name: method_code
    for method_code, method_name in ADMISSION_METHOD_CODE_TO_NAME.items()
}


def _clean_admission_method_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""

    return value.strip().strip("\"'").strip(" -*\u2022,;:")


def _admission_method_code_from_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return ADMISSION_METHOD_NAME_TO_CODE.get(value.strip())


def _admission_method_candidates_from_value(value: Any) -> list[str]:
    candidates: list[str] = []

    def add_candidate(raw_value: Any) -> None:
        cleaned = _clean_admission_method_text(raw_value)
        if cleaned:
            candidates.append(cleaned)

    if isinstance(value, str):
        add_candidate(value)
    elif isinstance(value, list):
        for item in value:
            add_candidate(item)

    deduped_candidates: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = candidate.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped_candidates.append(candidate)
    return deduped_candidates


def _admission_method_exists(methods: list[dict[str, str]], method_code: str, method_name: str) -> bool:
    target_name = method_name.strip()
    for existing_method in methods:
        for existing_code, existing_name in existing_method.items():
            if existing_code == method_code:
                return True
            if target_name and str(existing_name).strip() == target_name:
                return True
    return False


def _resolve_detected_admission_methods(
    method_value: Any,
    current_methods: list[dict[str, str]],
) -> list[dict[str, str]]:
    new_methods: list[dict[str, str]] = []
    current_year = datetime.now().year

    for raw_method in _admission_method_candidates_from_value(method_value):
        entities = search_entity_by_name(
            [raw_method],
            entity_type=["AdmissionMethod"],
            top_k=1,
            threshold=0.6,
            time={"from_year": str(current_year), "to_year": None},
            keywords=[raw_method]
        )
        if not entities:
            logger.info("Skipping admission method because Milvus returned no match: %s", raw_method)
            continue

        method_name = str(entities[0].get("node_name") or "").strip()
        method_code = _admission_method_code_from_name(method_name)
        if not method_name or not method_code:
            logger.info("Skipping admission method because Milvus name has no configured code mapping: %s", method_name)
            continue

        if not _admission_method_exists(current_methods, method_code, method_name) and not _admission_method_exists(
            new_methods, method_code, method_name
        ):
            new_methods.append({method_code: method_name})

    return new_methods


def _normalize_major_key(value: Any) -> str:
    text = _clean_major_text(value)
    if not text:
        return ""

    text = re.sub(r"^(ngành|chuyên ngành)\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*\([^)]*\)\s*$", "", text)
    text = re.sub(r"\s+", " ", text).strip().casefold()
    return _strip_accents(text)


def _major_search_terms(value: Any) -> list[str]:
    cleaned = _clean_major_text(value)
    if not cleaned:
        return []

    terms = [cleaned]
    without_prefix = re.sub(r"^(ngành|chuyên ngành)\s+", "", cleaned, flags=re.IGNORECASE).strip()
    if without_prefix and without_prefix != cleaned:
        terms.append(without_prefix)

    deduped_terms: list[str] = []
    seen: set[str] = set()
    for term in terms:
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped_terms.append(term)
    return deduped_terms


def _major_exists(majors: list[dict[str, str]], major_code: str, major_name: str) -> bool:
    normalized_target = _normalize_major_key(major_name)
    for existing_major in majors:
        for existing_code, existing_name in existing_major.items():
            if major_code != "UNKNOWN" and existing_code == major_code:
                return True
            if normalized_target and _normalize_major_key(existing_name) == normalized_target:
                return True
    return False


def _turn_entity_exists(
    turn_entities: list[dict[str, str]],
    entity_code: str,
    entity_name: str,
    entity_type: str,
) -> bool:
    normalized_target = _normalize_major_key(entity_name)
    for entity in turn_entities:
        existing_code = str(entity.get("entity_code", ""))
        existing_name = str(entity.get("entity_name", ""))
        existing_type = str(entity.get("entity_type", ""))
        if entity_code != "UNKNOWN" and existing_code == entity_code and existing_type == entity_type:
            return True
        if normalized_target and _normalize_major_key(existing_name) == normalized_target and existing_type == entity_type:
            return True
    return False


def _resolve_detected_major_entities(
    major_value: Any,
    current_majors: list[dict[str, str]],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    new_majors: list[dict[str, str]] = []
    turn_major_entities: list[dict[str, str]] = []

    detected_majors = []
    if major_value:
        if isinstance(major_value, str):
            detected_majors.append(major_value)
        elif isinstance(major_value, list):
            detected_majors.extend(major_value)

    for m_name in detected_majors:
        cleaned_major_name = _clean_major_text(m_name)
        if not cleaned_major_name:
            continue

        entities = []
        for search_name in _major_search_terms(cleaned_major_name):
            entities = search_entity_by_name(
                [search_name], entity_type=["Major", "Specialization"], top_k=1, threshold=0.6, keywords=[search_name]
            )
            if entities:
                break

        if not entities:
            logger.info("Skipping major because Milvus returned no match: %s", cleaned_major_name)
            continue

        matched_entity = entities[0]
        entity_type = str(matched_entity.get("node_type") or "Major")
        major_code = "UNKNOWN"
        major_name = matched_entity.get("node_name") or cleaned_major_name
        node_id = matched_entity.get("node_id")
        if not isinstance(node_id, str) or not node_id:
            node_id = ""

        node_attrs = neo4j_service.get_node_attribute(node_ids=[node_id], attributes_keys=["code"])
        logger.debug("node_attrs: %s", node_attrs)
        if isinstance(node_attrs, dict) and node_id:
            node_attr = node_attrs.get(node_id)
            if isinstance(node_attr, dict):
                major_code = str(node_attr.get("code", "UNKNOWN"))

        if not _turn_entity_exists(turn_major_entities, major_code, major_name, entity_type):
            turn_major_entities.append(
                {
                    "entity_code": major_code,
                    "entity_name": major_name,
                    "entity_type": entity_type,
                }
            )

        already_exists = _major_exists(current_majors, major_code, major_name)
        if not already_exists and not _major_exists(new_majors, major_code, major_name):
            new_majors.append({major_code: major_name})

    return new_majors, turn_major_entities


def _sanitize_current_stage_for_llm(current_stage: UserState) -> dict[str, Any]:
    sanitized_current_stage = normalize_for_json(current_stage)
    if isinstance(sanitized_current_stage, dict):
        for field in TURN_INTENT_FIELDS:
            sanitized_current_stage[field] = None
    return sanitized_current_stage


def stage_extractor(
    user_input: str,
    current_stage: UserState | None = None,
    last_agent_message: dict[str, Any] | None = None,
    return_metadata: bool = False,
) -> UserState | tuple[UserState, dict[str, Any]]:
    """
    Detect user state from input message.
    Only uses LLM JSON response. No keyword fallback, no heuristics.
    """
    try:
        current_stage = current_stage or UserState()
        current_student_profile = current_stage.student_profile or PersonalInfo()

        if not user_input or not isinstance(user_input, str):
            return (current_stage, {"turn_major_entities": []}) if return_metadata else current_stage

        json_input = {
            "current_stage": _sanitize_current_stage_for_llm(current_stage),
            "user_message": user_input if user_input else {},
            "last_agent_message": last_agent_message,
        }
        logger.info(f"stage_extractor input: {json_input}")
        # --- CALL LLM ---
        _s = _get_settings()
        _cfg = get_litellm_config(_s.litellm_stage_extractor_model, task="json_extraction")
        _cfg["max_tokens"] = 3000  # override — stage extractor chỉ cần output ngắn
        response = litellm.completion(
            **_cfg,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(json_input)},
            ],
            num_retries=5,
        )
        content = response.choices[0].message.content
        if not content:
            return (current_stage, {"turn_major_entities": []}) if return_metadata else current_stage

        logger.info(f"stage_extractor LLM content: {content}")

        # --- EXTRACT JSON ONLY ---
        json_match = re.search(r"\{.*}", content, re.DOTALL)
        if not json_match:
            return (current_stage, {"turn_major_entities": []}) if return_metadata else current_stage

        data = json.loads(json_match.group())
        current_topic = list(current_stage.topic) if current_stage.topic else []
        logger.info(f"stage_extractor raw data: {data}")

        # --- PARSE FIELDS FROM LLM ---
        role_value = data.get("role", None)
        topic_value = data.get("topic", "")
        logger.info(f"Topic value {topic_value}")
        if topic_value == "spam":
            is_spam = True
        else:
            is_spam = False
            if topic_value:
                if isinstance(topic_value, str):
                    if topic_value not in current_topic:
                        current_topic.append(topic_value)
                elif isinstance(topic_value, list):
                    for t in topic_value:
                        if t not in current_topic:
                            current_topic.append(t)

        major_value = data.get("major")
        admission_method_value = data.get("admission_methods", None)
        full_name = data.get("full_name", None)
        phone = data.get("phone", None)
        email = data.get("email", None)
        high_school = data.get("high_school", None)
        high_school_province = data.get("high_school_province", None)
        high_school_address = data.get("high_school_address", None)
        gender = data.get("gender", None)
        date_of_birth = data.get("date_of_birth", None)
        province_location = data.get("province_location", None)
        raw_high_school_province = (
            high_school_province.strip()
            if isinstance(high_school_province, str) and high_school_province.strip()
            else None
        )
        raw_province_location = (
            province_location.strip()
            if isinstance(province_location, str) and province_location.strip()
            else None
        )
        if _is_copied_current_province(
            raw_province_value=raw_high_school_province,
            current_province_value=current_student_profile.high_school_province,
            user_input=user_input,
        ):
            raw_high_school_province = None
            high_school_province = None
        if _is_copied_current_province(
            raw_province_value=raw_province_location,
            current_province_value=current_student_profile.province_location,
            user_input=user_input,
        ):
            raw_province_location = None
            province_location = None

        if high_school_province:
            high_school_province = normalize_province(high_school_province)
        if province_location:
            province_location = normalize_province(province_location)

        final_province_location = province_location or current_student_profile.province_location
        final_high_school_province_p63 = high_school_province or current_student_profile.high_school_province
        derived_province_location_from_school = False
        if not final_province_location and final_high_school_province_p63:
            final_province_location = normalize_province(final_high_school_province_p63)
            derived_province_location_from_school = True
            logger.info(
                f"Derived province_location from high_school_province: {final_high_school_province_p63} -> {final_province_location}"
            )
        final_old_province_location = raw_province_location
        if not final_old_province_location and derived_province_location_from_school:
            final_old_province_location = raw_high_school_province

        final_high_school_province = final_high_school_province_p63
        if final_high_school_province_p63:
            p34 = get_province_34(final_high_school_province_p63)
            if p34:
                final_high_school_province = p34

        if final_province_location:
            p34_loc = get_province_34(final_province_location)
            if p34_loc:
                final_province_location = p34_loc

        ward_location = data.get("ward_location", None)
        address_detail = data.get("address_detail", None)
        personalization_value = data.get("personalization", [])
        current_personalization = list(current_stage.personalization) if current_stage.personalization else []
        is_requested_advise_major = data.get("is_requested_advise_major", None)
        is_requested_advisor = data.get("is_requested_advisor", None)
        is_confirm_information_to_fill_form = data.get("is_confirm_information_to_fill_form", None)
        is_requested_submit_application_now = _coerce_bool(data.get("is_requested_submit_application_now", False))
        selected_school_option = data.get("selected_school_option", None)
        is_asking_agent_identity = data.get("is_asking_agent_identity", None)
        turn_intent_flags = {field: _coerce_bool(data.get(field, False)) for field in TURN_INTENT_FIELDS}
        # logger.info(f"Role for this user {role_value}")
        # --- CONVERT ENUMS ---
        role = UserRole(role_value) if role_value is not None else None

        # --- PROCESS MAJOR ---
        current_majors = list(current_stage.potential_majors or current_stage.major or [])
        new_majors, turn_major_entities = _resolve_detected_major_entities(major_value, current_majors)

        # Combine old and new majors
        final_majors = current_majors + new_majors

        current_admission_methods = list(current_student_profile.admission_methods or [])
        new_admission_methods = _resolve_detected_admission_methods(
            method_value=admission_method_value,
            current_methods=current_admission_methods,
        )
        final_admission_methods = current_admission_methods + new_admission_methods

        # --- PROCESS CONFIRMED MAJOR ---
        confirmed_select_major = current_stage.confirmed_select_major
        if is_confirm_information_to_fill_form and final_majors:
            # If user confirms, we assume the latest added major or the Last one is being confirmed
            # NOTE: This logic might need refinement based on explicit user selection if multiple majors exist.
            # For now, taking the last one as the "current topic" major.
            confirmed_select_major = final_majors[-1]

        current_student_profile = current_stage.student_profile or PersonalInfo()

        # --- MERGE WITH current_stage ---
        result = UserState(
            role=role or current_stage.role,
            is_spam=is_spam,
            topic=current_topic,
            major=final_majors,
            potential_majors=final_majors,
            interested_majors=list(current_stage.interested_majors or []),
            major_interest_events=list(current_stage.major_interest_events or []),
            major_interest_scores=list(current_stage.major_interest_scores or []),
            student_profile=PersonalInfo(
                full_name=full_name or current_student_profile.full_name,
                phone=phone or current_student_profile.phone,
                email=email or current_student_profile.email,
                gender=gender or current_student_profile.gender,
                date_of_birth=date_of_birth or current_student_profile.date_of_birth,
                province_location=final_province_location,
                old_province_location=(
                    final_old_province_location or current_student_profile.old_province_location
                ),
                ward_location=ward_location or current_student_profile.ward_location,
                high_school=high_school or current_student_profile.high_school,
                high_school_province=final_high_school_province,
                old_high_school_province=(
                    raw_high_school_province or current_student_profile.old_high_school_province
                ),
                high_school_address=high_school_address or current_student_profile.high_school_address,
                admission_methods=final_admission_methods,
                address_detail=address_detail or current_student_profile.address_detail,
            ),
            personalization=_merge_personalization(current_personalization, personalization_value),
            is_requested_advise_major=is_requested_advise_major or current_stage.is_requested_advise_major,
            is_requested_advisor=is_requested_advisor or current_stage.is_requested_advisor,
            is_confirm_information_to_fill_form=is_confirm_information_to_fill_form
            or current_stage.is_confirm_information_to_fill_form,
            is_requested_submit_application_now=is_requested_submit_application_now,
            confirmed_select_major=confirmed_select_major,
            selected_school_option=selected_school_option,
            is_asking_agent_identity=is_asking_agent_identity,
            **turn_intent_flags,
        )
        metadata = {"turn_major_entities": turn_major_entities}
        return (result, metadata) if return_metadata else result

    except Exception as e:
        traceback.print_exc()
        logger.error(f"stage extractor throw error: {e}")
        fallback_state = current_stage or UserState()
        return (fallback_state, {"turn_major_entities": []}) if return_metadata else fallback_state
