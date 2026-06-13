from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

TURN_INTENT_FIELDS = (
    "is_asking_admission_criteria",
    "is_asking_comparison",
    "is_asking_roadmap",
    "is_asking_application_process",
    "is_asking_deadline",
)

STAGE_RANK = {
    "prospecting": 0,
    "qualifying": 1,
    "advising": 2,
    "decision": 3,
    "enrollment": 4,
    "expension": 5
}

STATUS_RANK = {
    "prospecting": {"contacted": 0, "engaged": 1, "interested": 2},
    "qualifying": {"consulted": 0, "selected": 1, "submitted": 2, "clarified": 3},
    "advising": {"pending_approval": 0, "approved": 1, "reviewed": 2, "not_approved": 3, "selected_for_offer": 4},
    "decision": {"admitted": 0, "confirmed": 1, "paid": 2, "need_to_recycle/re-engage": 3},
    "enrollment": {"enrolled": 0, "oriented": 1, "onboarded": 2},
    "expension": {"studied": 0, "graduated": 1, "re_enrolled": 2, "preserved": 3, "dismissed": 4, "major_transferred": 5, "alumni": 6},
}

FIRST_STATUS_BY_STAGE = {
    "prospecting": "contacted",
    "qualifying": "consulted",
    "advising": "pending_approval",
    "decision": "admitted",
    "enrollment": "enrolled",
    "expension": "studied",
}


def stage_rank(stage: str | None) -> int:
    if not stage:
        return -1

    normalized_stage = stage.lower()
    stage_rank = STAGE_RANK.get(normalized_stage, -1)
    return stage_rank


def rank(stage: str | None, status: str | None) -> tuple[int, int, int]:
    current_stage_rank = stage_rank(stage)
    if current_stage_rank < 0:
        return -1, -1, -1

    normalized_stage = stage.lower() if isinstance(stage, str) else ""
    normalized_status = status.lower().strip() if isinstance(status, str) else None
    stage_status_rank = STATUS_RANK.get(normalized_stage, {})
    has_specific_status = int(
        bool(normalized_status) and normalized_status in stage_status_rank
    )
    status_rank = stage_status_rank.get(normalized_status, -1) if normalized_status else -1
    return current_stage_rank, has_specific_status, status_rank


def normalize_candidate(candidate) -> tuple[None, None] | tuple[Any | None, Any | None] | tuple:
    if not candidate:
        return None, None

    if isinstance(candidate, Mapping):
        if "next_stage" in candidate:
            stage = candidate.get("next_stage")
        else:
            stage = candidate.get("stage")
        if "next_status" in candidate:
            status = candidate.get("next_status")
        else:
            status = candidate.get("status")
        return stage, status

    if isinstance(candidate, tuple) and len(candidate) == 2:
        return candidate

    return None, None


def max_stage_status(candidates: Iterable) -> tuple[str | None, str | None]:
    best_stage = None
    best_status = None
    best_rank = (-1, -1, -1)

    for candidate in candidates:
        stage, status = normalize_candidate(candidate)
        candidate_rank = rank(stage, status)
        if candidate_rank > best_rank:
            best_stage = stage
            best_status = status
            best_rank = candidate_rank

    return best_stage, best_status


def normalize_turn_candidate_for_persist(
    turn_candidate: Any, intent_flags: Mapping[str, bool]
) -> tuple[str | None, str | None]:
    stage, status = normalize_candidate(turn_candidate)
    if not stage or not has_turn_intent(intent_flags):
        return None, None

    normalized_stage = stage.lower() if isinstance(stage, str) else stage
    normalized_status = status.lower().strip() if isinstance(status, str) else ""

    if (
        intent_flags.get("is_asking_application_process") or intent_flags.get("is_asking_deadline")
    ) and stage_rank(normalized_stage) > stage_rank("qualifying"):
        return "qualifying", ""

    return normalized_stage, normalized_status


def extract_turn_intent_flags(user_state) -> dict[str, bool]:
    flags = {}
    for field in TURN_INTENT_FIELDS:
        if isinstance(user_state, Mapping):
            value = user_state.get(field, False)
        else:
            value = getattr(user_state, field, False)
        flags[field] = bool(value)
    return flags


def has_turn_intent(intent_flags: Mapping[str, bool]) -> bool:
    return any(bool(intent_flags.get(field)) for field in TURN_INTENT_FIELDS)


def should_use_turn_for_playbook(turn_candidate: Any, base_journey: Any) -> bool:
    turn_stage, _ = normalize_candidate(turn_candidate)
    base_stage, _ = normalize_candidate(base_journey)
    return stage_rank(turn_stage) > stage_rank(base_stage)


def hydrate_empty_status(stage: str | None, status: str | None) -> str | None:
    if isinstance(status, str) and status.strip():
        return status

    if not isinstance(stage, str):
        return status

    return FIRST_STATUS_BY_STAGE.get(stage.lower(), status)
