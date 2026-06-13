# data_collection_tools.py
"""Simplified Data Collection Tools - Extract keywords and call segment API."""

import logging
from typing import Any

import httpx
import requests
from google.adk.agents.callback_context import CallbackContext

from configs.config_service import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()

# Segment API Configuration
SEGMENT_API_URL = settings.segmentation_api_url
SEGMENT_API_TIMEOUT = settings.segmentation_api_timeout_seconds
CONFIDENCE_RANK = {
    "STRONG_ACCEPT": 3,
    "ACCEPT": 2,
    "REVIEW": 1,
    "LOW_CONFIDENCE": 0,
}


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_confidence_status(value: Any) -> str | None:
    if not isinstance(value, str):
        return None

    status = value.strip().upper()
    if not status:
        return None

    return status


def _has_segment_features(features: dict[str, Any]) -> bool:
    return any(bool(value) for value in features.values())


def _segment_confidence_rank(segment: dict[str, Any]) -> int:
    service_rank = _safe_float(segment.get("confidence_rank"))
    if service_rank is not None:
        return int(service_rank)

    return CONFIDENCE_RANK.get(_normalize_confidence_status(segment.get("confidence_status")) or "", 0)


def _select_top_segment(top_segments: list[dict[str, Any]]) -> dict[str, Any]:
    return max(
        top_segments,
        key=lambda s: (
            _safe_float(s.get("score")) or 0.0,
            _segment_confidence_rank(s),
            _safe_float(s.get("margin")) or 0.0,
            _safe_float(s.get("coverage")) or 0.0,
            _safe_float(s.get("normalized_score")) or 0.0,
        ),
    )


def _call_segment_api(
    features: dict[str, Any],
    tool_context: CallbackContext,
) -> str | None | Any:
    """
    Call segmentation API and return top segment name.
    Returns "Unknown" on any failure.
    """

    # tool_context.actions.skip_summarization = True
    if not features or not _has_segment_features(features):
        logger.warning("Segment API skipped: empty features")
        tool_context.state["segment_confidence_status"] = "LOW_CONFIDENCE"
        tool_context.state["segment_top_segments"] = []
        tool_context.state["segment"] = "Unknown"
        return "Unknown"

    customer_role = tool_context.state.get("user_state", {}).get("role", "student")
    api_customer_type = "Parents" if customer_role == "parent" else "Learner"
    tool_context.state["customer_type_used_for_segment_api"] = api_customer_type

    payload = {
        "customer_type": api_customer_type,
        "psychographic": {
            "motivation_keywords": features.get("motivation_keywords", []),
            "tuition_sensitive_keywords": features.get("tuition_sensitive_keywords", []),
            "core_values_keywords": features.get("core_values_keywords", []),
            "studying_goals_keywords": features.get("studying_goals_keywords", []),
            "environment_keywords": features.get("environment_keywords", []),
        },
        "behavioral": {
            "interaction_frequency_keywords": features.get("interaction_frequency_keywords", []),
            "preferred_channel_keywords": features.get("preferred_channel_keywords", []),
            "financial_behavior_keywords": features.get("financial_behavior_keywords", []),
            "churn_risk_signals_keywords": features.get("churn_risk_signals_keywords", []),
        },
        "top_k": 3,
    }

    try:
        response = requests.post(
            SEGMENT_API_URL,
            json=payload,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=SEGMENT_API_TIMEOUT,
        )

        logger.info("Segment API status_code=%s", response.status_code)

        if response.status_code != 200:
            logger.info(
                "Segment API error status=%s body=%s",
                response.status_code,
                response.text,
            )
            tool_context.state["segment_confidence_status"] = "LOW_CONFIDENCE"
            tool_context.state["segment_top_segments"] = []
            tool_context.state["segment"] = "Unknown"
            return "Unknown"

        data = response.json()
        top_segments = data.get("top_segments", [])

        if not top_segments:
            logger.info("Segment API returned no segments")
            tool_context.state["segment_confidence_status"] = "LOW_CONFIDENCE"
            tool_context.state["segment_top_segments"] = []
            tool_context.state["segment"] = "Unknown"
            return "Unknown"

        top_segment = _select_top_segment(top_segments)
        confidence_status = _normalize_confidence_status(top_segment.get("confidence_status")) or "LOW_CONFIDENCE"
        segment_name = (
            top_segment.get("name", "Unknown")
            if confidence_status in {"ACCEPT", "STRONG_ACCEPT"}
            else "Unknown"
        )
        logger.info(
            "Segment API top segment=%s confidence_status=%s final_segment=%s",
            top_segment.get("name", "Unknown"),
            confidence_status,
            segment_name,
        )
        tool_context.state["segment_confidence_status"] = confidence_status
        tool_context.state["segment_top_segments"] = top_segments
        tool_context.state["segment"] = segment_name

        return segment_name

    except (httpx.RequestError, requests.RequestException):
        logger.exception("Segment API network failure")
        tool_context.state["segment_confidence_status"] = "LOW_CONFIDENCE"
        tool_context.state["segment_top_segments"] = []
        tool_context.state["segment"] = "Unknown"
        return "Unknown"
    except ValueError:
        logger.exception("Segment API returned invalid JSON")
        tool_context.state["segment_confidence_status"] = "LOW_CONFIDENCE"
        tool_context.state["segment_top_segments"] = []
        tool_context.state["segment"] = "Unknown"
        return "Unknown"
    except Exception:
        logger.exception("Unexpected error calling Segment API")
        tool_context.state["segment_confidence_status"] = "LOW_CONFIDENCE"
        tool_context.state["segment_top_segments"] = []
        tool_context.state["segment"] = "Unknown"
        return "Unknown"
