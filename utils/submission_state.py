from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

SubmissionLikeRoute = Literal["pending_approval", "submitted"]

_ROUTE_CONFIG: dict[SubmissionLikeRoute, dict[str, str]] = {
    "pending_approval": {
        "flag_key": "is_pending_approval",
        "ack_key": "is_pending_approval_acknowledged",
        "stage": "advising",
        "status": "pending_approval",
    },
    "submitted": {
        "flag_key": "is_submitted",
        "ack_key": "is_submitted_acknowledged",
        "stage": "qualifying",
        "status": "submitted",
    },
}

_ROUTE_PRIORITY: tuple[SubmissionLikeRoute, ...] = ("pending_approval", "submitted")


def should_mark_crm_update_state(state_delta: Mapping[str, Any]) -> bool:
    return any(state_delta.get(_ROUTE_CONFIG[route]["flag_key"]) is True for route in _ROUTE_PRIORITY)


def get_active_submission_like_routes(state: Mapping[str, Any]) -> tuple[SubmissionLikeRoute, ...]:
    return tuple(route for route in _ROUTE_PRIORITY if state.get(_ROUTE_CONFIG[route]["flag_key"]) is True)


def get_submission_like_route(state: Mapping[str, Any]) -> SubmissionLikeRoute | None:
    active_routes = get_active_submission_like_routes(state)
    return active_routes[0] if active_routes else None


def get_submission_like_ack_key(route: SubmissionLikeRoute) -> str:
    return _ROUTE_CONFIG[route]["ack_key"]


def is_submission_like_acknowledged(state: Mapping[str, Any], route: SubmissionLikeRoute) -> bool:
    return state.get(get_submission_like_ack_key(route), False) is True


def get_submission_like_stage_status(route: SubmissionLikeRoute) -> tuple[str, str]:
    config = _ROUTE_CONFIG[route]
    return config["stage"], config["status"]
