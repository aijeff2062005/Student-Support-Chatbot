from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from typing import Any

import litellm

from configs.config_service import get_settings as _get_settings
from configs.llm_client import get_litellm_config
from prompts.data_collector.major_interest_signal_extractor import SYSTEM_PROMPT
from schemas.stage_variable import MajorInterestEvent, MajorInterestScore, UserState, combine_major_views

logger = logging.getLogger(__name__)

ALLOWED_SIGNAL_TYPES = {
    "explicit_choice",
    "strong_commitment",
    "preference",
    "comparative_preference",
    "soft_interest",
    "exploration",
    "uncertainty",
    "negative_preference",
    "replacement_choice",
    "hard_drop",
}
ALLOWED_POLARITIES = {"positive", "negative", "neutral"}
POSITIVE_SIGNAL_TYPES = {
    "explicit_choice",
    "strong_commitment",
    "preference",
    "comparative_preference",
    "soft_interest",
    "exploration",
    "uncertainty",
}
NEGATIVE_SIGNAL_TYPES = {"negative_preference", "replacement_choice"}
BASE_SIGNAL_SCORES = {
    "explicit_choice": 90,
    "strong_commitment": 97,
    "preference": 78,
    "comparative_preference": 76,
    "soft_interest": 70,
    "exploration": 40,
    "uncertainty": 50,
    "negative_preference": -45,
    "replacement_choice": 92,
    "hard_drop": -100,
}
SCORING_CUE_PATTERNS = (
    "ngành này",
    "ngành đó",
    "ngành kia",
    "cái này",
    "cái đó",
    "cái kia",
    "chọn",
    "chốt",
    "quyết định",
    "thích",
    "ưng",
    "nghiêng",
    "phân vân",
    "cân nhắc",
    "không thích",
    "không hợp",
    "bỏ",
    "thay vì",
    "hơn",
)


def _normalize_major_key(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip().strip("\"'").casefold()
    text = re.sub(r"^(ngành|chuyên ngành)\s+", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _default_polarity(signal_type: str) -> str:
    if signal_type in {"negative_preference", "hard_drop"}:
        return "negative"
    if signal_type in {"exploration", "uncertainty"}:
        return "neutral"
    return "positive"


def _extract_json_payload(content: str) -> dict[str, Any] | None:
    if not isinstance(content, str) or not content.strip():
        return None

    cleaned_content = content.replace("```json", "").replace("```", "").strip()
    try:
        payload = json.loads(cleaned_content)
        return payload if isinstance(payload, dict) else None
    except json.JSONDecodeError:
        json_match = re.search(r"\{.*}", cleaned_content, re.DOTALL)
        if not json_match:
            return None
        try:
            payload = json.loads(json_match.group())
            return payload if isinstance(payload, dict) else None
        except json.JSONDecodeError:
            return None


def _has_major_interest_cue(user_message: str) -> bool:
    if not isinstance(user_message, str):
        return False
    lowered = user_message.casefold()
    return any(cue in lowered for cue in SCORING_CUE_PATTERNS)


def should_run_major_interest_scoring(
    user_message: str,
    current_state: UserState,
    turn_entities: list[dict[str, str]],
) -> bool:
    if turn_entities:
        return True

    has_known_entities = bool(
        current_state.potential_majors or current_state.interested_majors or current_state.major_interest_scores
    )
    if not has_known_entities:
        return False

    return _has_major_interest_cue(user_message)


def _build_entity_catalog(
    turn_entities: list[dict[str, str]],
    current_state: UserState,
) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    code_catalog: dict[str, dict[str, str]] = {}
    name_catalog: dict[str, dict[str, str]] = {}

    def register(entity_code: str, entity_name: str, entity_type: str) -> None:
        if not entity_name:
            return
        entry = {
            "entity_code": str(entity_code or "UNKNOWN"),
            "entity_name": entity_name,
            "entity_type": entity_type or "Major",
        }
        if entry["entity_code"]:
            code_catalog[entry["entity_code"]] = entry
        normalized_name = _normalize_major_key(entry["entity_name"])
        if normalized_name:
            name_catalog[normalized_name] = entry

    for entity in turn_entities or []:
        register(
            str(entity.get("entity_code", "UNKNOWN")),
            str(entity.get("entity_name", "")),
            str(entity.get("entity_type", "Major")),
        )

    for score in current_state.major_interest_scores or []:
        register(score.entity_code, score.entity_name, score.entity_type)

    return code_catalog, name_catalog


def _resolve_entity_reference(
    entity_code: Any,
    entity_name: Any,
    code_catalog: dict[str, dict[str, str]],
    name_catalog: dict[str, dict[str, str]],
) -> dict[str, str] | None:
    code_key = str(entity_code or "").strip()
    if code_key and code_key in code_catalog:
        return code_catalog[code_key]

    normalized_name = _normalize_major_key(entity_name)
    if normalized_name and normalized_name in name_catalog:
        return name_catalog[normalized_name]
    return None


def extract_major_interest_signals(
    user_message: str,
    last_agent_message: dict[str, Any] | None,
    conversation_turns: list[dict[str, Any]] | None,
    current_state: UserState,
    turn_entities: list[dict[str, str]],
) -> list[dict[str, Any]]:
    code_catalog, name_catalog = _build_entity_catalog(turn_entities, current_state)
    json_input = {
        "user_message": user_message,
        "last_agent_message": last_agent_message or {},
        # "conversation_turns": list(conversation_turns or [])[-5:],
        "turn_entities": turn_entities,
        "current_potential_majors": list(current_state.potential_majors or []),
        "current_interested_majors": list(current_state.interested_majors or []),
        "current_major_interest_scores": [
            {
                "entity_code": score.entity_code,
                "entity_name": score.entity_name,
                "entity_type": score.entity_type,
                "interest_score": score.interest_score,
                "status": score.status,
                "in_interested_majors": score.in_interested_majors,
            }
            for score in list(current_state.major_interest_scores or [])[:5]
        ],
    }
    logger.info("major_interest_signal_extractor input: %s", json_input)

    settings = _get_settings()
    llm_config = get_litellm_config(settings.litellm_stage_extractor_model, task="json_extraction")
    llm_config["max_tokens"] = 500
    response = litellm.completion(
        **llm_config,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(json_input, ensure_ascii=False)},
        ],
        num_retries=3,
    )
    content = response.choices[0].message.content if response and response.choices else None
    payload = _extract_json_payload(content or "")
    if not payload:
        return []

    raw_signals = payload.get("signals", [])
    if not isinstance(raw_signals, list):
        return []

    validated_signals: list[dict[str, Any]] = []
    seen_markers: set[tuple[str, str, str, str]] = set()
    for raw_signal in raw_signals:
        if not isinstance(raw_signal, dict):
            continue

        signal_type = str(raw_signal.get("signal_type", "")).strip()
        if signal_type not in ALLOWED_SIGNAL_TYPES:
            continue

        if raw_signal.get("applies_to_current_turn_only") is False:
            continue

        entity = _resolve_entity_reference(
            raw_signal.get("entity_code"),
            raw_signal.get("entity_name"),
            code_catalog,
            name_catalog,
        )
        if not entity:
            continue

        polarity = str(raw_signal.get("polarity") or _default_polarity(signal_type)).strip().lower()
        if polarity not in ALLOWED_POLARITIES:
            polarity = _default_polarity(signal_type)

        confidence = raw_signal.get("confidence", 0.0)
        try:
            confidence = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            confidence = 0.0

        preference_order = raw_signal.get("preference_order")
        if preference_order is not None:
            try:
                preference_order = int(preference_order)
            except (TypeError, ValueError):
                continue
            if preference_order <= 0:
                continue

        related_entity = _resolve_entity_reference(
            raw_signal.get("related_entity_code"),
            raw_signal.get("related_entity_name"),
            code_catalog,
            name_catalog,
        )

        signal_payload = {
            "entity_code": entity["entity_code"],
            "entity_name": entity["entity_name"],
            "entity_type": entity["entity_type"],
            "signal_type": signal_type,
            "polarity": polarity,
            "preference_order": preference_order,
            "confidence": confidence,
            "evidence": raw_signal.get("evidence"),
            "related_entity_code": related_entity["entity_code"] if related_entity else None,
            "related_entity_name": related_entity["entity_name"] if related_entity else None,
            "related_entity_type": related_entity["entity_type"] if related_entity else None,
        }
        marker = (
            signal_payload["entity_code"],
            signal_payload["signal_type"],
            signal_payload["polarity"],
            signal_payload["related_entity_code"] or "",
        )
        if marker in seen_markers:
            continue
        seen_markers.add(marker)
        validated_signals.append(signal_payload)

    return validated_signals


def _fallback_events(
    turn_entities: list[dict[str, str]],
    turn_index: int,
    user_message: str,
) -> list[MajorInterestEvent]:
    fallback_events = []
    for entity in turn_entities:
        fallback_events.append(
            MajorInterestEvent(
                entity_code=str(entity.get("entity_code", "UNKNOWN")),
                entity_name=str(entity.get("entity_name", "")),
                entity_type=str(entity.get("entity_type", "Major")),
                signal_type="exploration",
                polarity="neutral",
                confidence=0.35,
                evidence=user_message[:120],
                turn_index=turn_index,
                source_text=user_message,
                notes="fallback_on_llm_failure",
            )
        )
    return fallback_events


def build_major_interest_events(
    validated_signals: list[dict[str, Any]],
    turn_index: int,
    user_message: str,
) -> list[MajorInterestEvent]:
    events: list[MajorInterestEvent] = []
    for signal in validated_signals:
        events.append(
            MajorInterestEvent(
                entity_code=signal["entity_code"],
                entity_name=signal["entity_name"],
                entity_type=signal["entity_type"],
                signal_type=signal["signal_type"],
                polarity=signal["polarity"],
                preference_order=signal.get("preference_order"),
                confidence=signal["confidence"],
                evidence=signal.get("evidence"),
                turn_index=turn_index,
                source_text=user_message,
                related_entity_code=signal.get("related_entity_code"),
                related_entity_name=signal.get("related_entity_name"),
            )
        )

        if (
            signal["signal_type"] == "replacement_choice"
            and signal["polarity"] == "positive"
            and signal.get("related_entity_code")
            and signal.get("related_entity_name")
        ):
            events.append(
                MajorInterestEvent(
                    entity_code=signal["related_entity_code"],
                    entity_name=signal["related_entity_name"],
                    entity_type=signal.get("related_entity_type") or signal["entity_type"],
                    signal_type="replacement_choice",
                    polarity="negative",
                    preference_order=None,
                    confidence=signal["confidence"],
                    evidence=signal.get("evidence"),
                    turn_index=turn_index,
                    source_text=user_message,
                    related_entity_code=signal["entity_code"],
                    related_entity_name=signal["entity_name"],
                    notes="replacement_choice_demoted_entity",
                )
            )
    return events


def _major_list_contains(majors: list[dict[str, str]], entity_code: str, entity_name: str) -> bool:
    normalized_target = _normalize_major_key(entity_name)
    for major in majors or []:
        if not isinstance(major, dict):
            continue
        for existing_code, existing_name in major.items():
            if entity_code != "UNKNOWN" and str(existing_code) == entity_code:
                return True
            if normalized_target and _normalize_major_key(existing_name) == normalized_target:
                return True
    return False


def _score_status(score: int) -> str:
    if score >= 85:
        return "confirmed_interest"
    if score >= 70:
        return "preferred"
    if score >= 50:
        return "considering"
    if score >= 30:
        return "exploring"
    return "weak_signal"


def _build_reason_summary(
    top_signal_type: str | None,
    confirmation_boost: int,
    comparison_boost: int,
    negative_penalty: int,
    uncertainty_penalty: int,
    was_previously_interested: bool,
    hard_drop_active: bool,
    fallback_used: bool,
) -> str:
    reasons = []
    if hard_drop_active:
        reasons.append("Hard drop ở lượt gần nhất")
    elif top_signal_type:
        reasons.append(f"Top signal: {top_signal_type}")
    if confirmation_boost:
        reasons.append("có xác nhận mạnh")
    if comparison_boost:
        reasons.append("có ưu tiên so sánh")
    if uncertainty_penalty:
        reasons.append("có phân vân ở lượt gần nhất")
    if negative_penalty:
        reasons.append(f"bị giảm bởi tín hiệu phủ định ({negative_penalty})")
    if was_previously_interested and not hard_drop_active:
        reasons.append("giữ trạng thái sticky")
    if fallback_used:
        reasons.append("dùng fallback exploration")
    return "; ".join(reasons) if reasons else "Chưa có tín hiệu đủ mạnh"


def aggregate_major_interest_scores(
    events: list[MajorInterestEvent],
    previous_interested_majors: list[dict[str, str]],
    current_turn_index: int,
) -> list[MajorInterestScore]:
    grouped_events: dict[tuple[str, str, str], list[MajorInterestEvent]] = defaultdict(list)
    for event in events:
        grouped_events[(event.entity_code, event.entity_name, event.entity_type)].append(event)

    scores: list[MajorInterestScore] = []
    for (entity_code, entity_name, entity_type), entity_events in grouped_events.items():
        entity_events = sorted(entity_events, key=lambda item: (item.turn_index or 0, item.entity_code, item.signal_type))
        positive_events = [
            event
            for event in entity_events
            if event.signal_type in POSITIVE_SIGNAL_TYPES
            or (event.signal_type == "replacement_choice" and event.polarity == "positive")
        ]
        latest_turn = max((event.turn_index or 0) for event in entity_events)
        latest_positive_turn = max((event.turn_index or 0) for event in positive_events) if positive_events else -1
        latest_turn_events = [event for event in entity_events if (event.turn_index or 0) == latest_turn]
        hard_drop_turn = max(
            ((event.turn_index or 0) for event in entity_events if event.signal_type == "hard_drop"),
            default=-1,
        )
        hard_drop_active = hard_drop_turn > latest_positive_turn

        if hard_drop_active:
            final_score = 0
            top_signal_type = "hard_drop"
            confirmation_boost = 0
            comparison_boost = 0
            uncertainty_penalty = 0
            negative_penalty = 0
        else:
            ranked_preference_events = [
                event
                for event in entity_events
                if event.signal_type == "explicit_choice"
                and event.polarity == "positive"
                and isinstance(event.preference_order, int)
                and event.preference_order > 0
            ]

            if ranked_preference_events:
                best_ranked_event = min(
                    ranked_preference_events,
                    key=lambda event: (event.preference_order or 10**9, -(event.turn_index or 0)),
                )
                final_score = max(0, 101 - int(best_ranked_event.preference_order))
                top_signal_type = "explicit_choice"
                confirmation_boost = 0
                comparison_boost = 0
                uncertainty_penalty = 0
                negative_penalty = 0
            else:
                top_signal_type = None
                base_peak = 0
                for event in positive_events:
                    base_value = BASE_SIGNAL_SCORES.get(event.signal_type, 0)
                    if base_value >= base_peak:
                        base_peak = base_value
                        top_signal_type = event.signal_type

                positive_event_count = len(positive_events)
                repetition_boost = min(max(positive_event_count - 1, 0) * 3, 9)

                recent_positive_turns = {
                    event.turn_index
                    for event in positive_events
                    if event.turn_index is not None and event.turn_index >= max(current_turn_index - 2, 1)
                }
                recency_boost = 8 if len(recent_positive_turns) >= 3 else 4 if len(recent_positive_turns) >= 2 else 0

                comparison_boost = 8 if any(event.signal_type == "comparative_preference" for event in entity_events) else 0
                confirmation_boost = 0
                for event in entity_events:
                    if event.signal_type not in {"explicit_choice", "strong_commitment"}:
                        continue
                    if any(
                        prior_event.signal_type in POSITIVE_SIGNAL_TYPES
                        and prior_event.signal_type not in {"explicit_choice", "strong_commitment"}
                        and (prior_event.turn_index or 0) < (event.turn_index or 0)
                        for prior_event in entity_events
                    ):
                        confirmation_boost = 10
                        break

                uncertainty_penalty = 5 if any(event.signal_type == "uncertainty" for event in latest_turn_events) else 0
                negative_penalty = min(
                    sum(
                        abs(BASE_SIGNAL_SCORES.get(event.signal_type, 0))
                        for event in entity_events
                        if event.signal_type in NEGATIVE_SIGNAL_TYPES
                        and event.polarity == "negative"
                        and (event.turn_index or 0) > latest_positive_turn
                    ),
                    80,
                )
                final_score = max(
                    0,
                    min(
                        100,
                        base_peak
                        + repetition_boost
                        + recency_boost
                        + comparison_boost
                        + confirmation_boost
                        - uncertainty_penalty
                        - negative_penalty,
                    ),
                )

        was_previously_interested = _major_list_contains(previous_interested_majors, entity_code, entity_name)
        in_interested_majors = False if hard_drop_active else (was_previously_interested or final_score >= 80)
        fallback_used = any(event.notes == "fallback_on_llm_failure" for event in latest_turn_events)
        reason_summary = _build_reason_summary(
            top_signal_type=top_signal_type,
            confirmation_boost=confirmation_boost,
            comparison_boost=comparison_boost,
            negative_penalty=negative_penalty,
            uncertainty_penalty=uncertainty_penalty,
            was_previously_interested=was_previously_interested,
            hard_drop_active=hard_drop_active,
            fallback_used=fallback_used,
        )
        scores.append(
            MajorInterestScore(
                entity_code=entity_code,
                entity_name=entity_name,
                entity_type=entity_type,
                interest_score=int(final_score),
                status=_score_status(int(final_score)),
                rank=0,
                event_count=len(entity_events),
                last_turn_index=latest_turn or None,
                in_interested_majors=in_interested_majors,
                reason_summary=reason_summary,
            )
        )

    scores.sort(
        key=lambda score: (
            -score.interest_score,
            -(score.last_turn_index or 0),
            score.entity_name.casefold(),
        )
    )
    for index, score in enumerate(scores, start=1):
        score.rank = index
    return scores


def derive_interested_majors(scores: list[MajorInterestScore]) -> list[dict[str, str]]:
    interested = []
    for score in scores:
        if not score.in_interested_majors:
            continue
        interested.append({score.entity_code: score.entity_name})
    return interested


def derive_potential_majors(
    potential_majors: list[dict[str, str]],
    interested_majors: list[dict[str, str]],
) -> list[dict[str, str]]:
    interested_keys = set()
    for major_item in interested_majors:
        if not isinstance(major_item, dict):
            continue
        for entity_code, entity_name in major_item.items():
            interested_keys.add((_normalize_major_key(entity_code), _normalize_major_key(entity_name)))

    remaining = []
    seen = set()
    for major_item in potential_majors:
        if not isinstance(major_item, dict):
            continue
        for entity_code, entity_name in major_item.items():
            normalized_key = (_normalize_major_key(entity_code), _normalize_major_key(entity_name))
            if normalized_key in interested_keys or normalized_key in seen:
                continue
            remaining.append({entity_code: entity_name})
            seen.add(normalized_key)
    return remaining


def apply_major_interest_scoring(
    current_state: UserState,
    previous_state: UserState,
    user_message: str,
    last_agent_message: dict[str, Any] | None,
    conversation_turns: list[dict[str, Any]] | None,
    turn_entities: list[dict[str, str]],
    current_turn_index: int,
) -> UserState:
    current_state.potential_majors = list(current_state.potential_majors or current_state.major or [])
    current_state.major = combine_major_views(current_state.potential_majors, current_state.interested_majors)

    if not current_state.potential_majors:
        current_state.major_interest_events = []
        current_state.major_interest_scores = []
        current_state.interested_majors = []
        return current_state

    if not should_run_major_interest_scoring(user_message, current_state, turn_entities):
        current_state.major_interest_events = list(previous_state.major_interest_events or [])
        current_state.major_interest_scores = list(previous_state.major_interest_scores or [])
        current_state.interested_majors = list(previous_state.interested_majors or [])
        current_state.potential_majors = derive_potential_majors(
            potential_majors=list(current_state.potential_majors),
            interested_majors=list(current_state.interested_majors),
        )
        current_state.major = combine_major_views(current_state.potential_majors, current_state.interested_majors)
        return current_state

    try:
        validated_signals = extract_major_interest_signals(
            user_message=user_message,
            last_agent_message=last_agent_message,
            conversation_turns=conversation_turns,
            current_state=current_state,
            turn_entities=turn_entities,
        )
    except Exception as exc:
        logger.error("major_interest_signal_extractor failed: %s", exc)
        validated_signals = []

    turn_events = build_major_interest_events(validated_signals, current_turn_index, user_message)
    if not turn_events and turn_entities:
        turn_events = _fallback_events(turn_entities, current_turn_index, user_message)

    all_events = list(previous_state.major_interest_events or []) + turn_events
    scores = aggregate_major_interest_scores(
        events=all_events,
        previous_interested_majors=list(previous_state.interested_majors or []),
        current_turn_index=current_turn_index,
    )
    current_state.major_interest_events = all_events
    current_state.major_interest_scores = scores
    current_state.interested_majors = derive_interested_majors(scores)
    current_state.potential_majors = derive_potential_majors(
        potential_majors=list(current_state.potential_majors),
        interested_majors=list(current_state.interested_majors),
    )
    current_state.major = combine_major_views(current_state.potential_majors, current_state.interested_majors)
    return current_state
