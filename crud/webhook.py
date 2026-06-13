import logging
import time
from typing import Any

from admission_agent_service import get_admission_agent_service

logger = logging.getLogger(__name__)


def _clean_greeting_name(value: Any) -> str:
    """Normalize candidate display names for greeting responses."""
    if not isinstance(value, str):
        return ""
    return value.strip()


def resolve_greeting_display_name(
    user_id: str,
    customer_info: dict[str, Any] | None = None,
    session_state: dict[str, Any] | None = None,
) -> str:
    """Resolve the display name for the default greeting with stable fallbacks."""
    customer_info = customer_info or {}
    session_state = session_state or {}

    for candidate in (
        customer_info.get("full_name"),
        session_state.get("customer_info", {}).get("name"),
        session_state.get("customer_info", {}).get("full_name"),
        session_state.get("student_name"),
    ):
        cleaned_name = _clean_greeting_name(candidate)
        if cleaned_name:
            return cleaned_name

    return ""


def build_default_greeting_message(
    user_id: str,
    customer_info: dict[str, Any] | None = None,
    session_state: dict[str, Any] | None = None,
) -> str:
    """Build the standard first-contact greeting shared by webhook and WebSocket flows."""
    display_name = resolve_greeting_display_name(
        user_id=user_id,
        customer_info=customer_info,
        session_state=session_state,
    )
    return (
        f"Xin chào {display_name}, tôi là trợ lý về công tác tuyển sinh và trải nghiệm sinh viên của "
        "Đại học Gia Định. Tôi có thể giúp gì cho bạn hôm nay?"
    )


async def process_webhook_admission(
    user_id: str,
    session_id: str,
    message: str,
    customer_info: dict[str, Any] | None = None,
    sale_profile: dict[str, Any] | None = None,
    platform: str | None = None,
    attachments: Any | None = None,
) -> dict[str, str] | dict[str, str | Any] | list[dict[str, str] | dict[str, str | Any]]:
    """
    Process admission query via webhook (non-streaming).

    Collects all streaming chunks until status="completed",
    then returns complete response.

     STANDARDIZED NAMING:
    - user_id: User identifier
    - session_id: Session/conversation ID

    Args:
            user_id: User identifier (standardized field name)
            session_id: Session/conversation ID (standardized field name)
            message: User query message
            customer_info
            sale_profile
            platform

    Returns:
            Dict with complete response when status="completed"
    """
    admission_service = await get_admission_agent_service()

    # Collect streaming chunks until completed
    accumulated_responses = []

    timeout_seconds = 180
    start_time = time.time()

    try:
        async for chunk in admission_service.stream_query_admission(
            user_id=user_id,
            message=message,
            session_id=session_id,
            customer_info=customer_info,
            sale_profile=sale_profile,
            platform=platform,
            attachments=attachments,
        ):
            # Timeout protection
            if time.time() - start_time > timeout_seconds:
                logger.warning(f" Webhook timeout after {timeout_seconds}s")
                return {
                    "status": "error",
                    "message": "Request timeout after {timeout_seconds} seconds",
                    "user_id": user_id,
                    "session_id": session_id,
                }

            # Handle error
            if chunk.get("event") == "error":
                logger.error(f"Agent error: {chunk.get('data')}")
                return {
                    "status": "error",
                    "message": chunk.get("data", "Unknown error"),
                    "user_id": user_id,
                    "session_id": session_id,
                }

            # Handle COMPLETED events for each agent
            if chunk.get("status") == "completed":
                # Add the FULL chunk to list (mirroring streaming output)
                # This ensures the webhook client receives the exact same structure as the streaming client
                accumulated_responses.append(chunk)

        # End of stream iteration

        # If no responses collected
        if not accumulated_responses:
            logger.warning(" No completed responses received from streaming")
            return {
                "status": "error",
                "message": "Processing incomplete - no final response",
                "user_id": user_id,
                "session_id": session_id,
            }

        # Construct Final Webhook Response
        # logger.error(f"total message chunks received: {len(accumulated_responses)}")
        # logger.error(accumulated_responses)
        return accumulated_responses

    except Exception as e:
        logger.error(f"Webhook processing error: {e}")
        import traceback

        logger.error(traceback.format_exc())

        return {
            "status": "error",
            "message": get_webhook_friendly_error(e),
            "user_id": user_id,
            "session_id": session_id,
        }


def get_webhook_friendly_error(error: Exception) -> str:
    """Convert technical errors to user-friendly messages."""
    error_str = str(error).lower()

    if "timeout" in error_str:
        return "Request processing timeout. Please try with a shorter message."
    elif "connection" in error_str or "network" in error_str:
        return "Service connection error. Please retry in a few minutes."
    elif "mcp" in error_str or "server" in error_str:
        return "Knowledge database temporarily unavailable. Please retry later."
    elif "session" in error_str:
        return "Session management error. Please retry with the same session ID."
    elif "agent" in error_str:
        return "AI agent service temporarily unavailable. Please retry."
    else:
        return "Internal processing error. Please contact system administrator."
