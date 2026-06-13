import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

_EVENTS_SCHEMA_V0 = "v0"
_EVENTS_SCHEMA_V1 = "v1"


async def _detect_events_schema(db: AsyncSession) -> str:
    result = await db.execute(
        text(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'events'
            """
        )
    )
    columns = {row[0] for row in result.fetchall()}
    if "event_data" in columns:
        return _EVENTS_SCHEMA_V1
    if "content" in columns:
        return _EVENTS_SCHEMA_V0
    raise RuntimeError("Unsupported ADK events schema: missing event_data/content columns")


def _extract_original_message(content: dict[str, Any] | None) -> str:
    if not content or "parts" not in content:
        return ""

    for part in content["parts"]:
        if "text" in part:
            return part["text"]
    return ""


def _build_edit_info(editor_info: dict | None, reason: str | None) -> dict[str, Any]:
    edit_info: dict[str, Any] = {}
    if editor_info:
        if editor_info.get("consultant_id"):
            edit_info["consultant_id"] = editor_info["consultant_id"]
        if editor_info.get("consultant_name"):
            edit_info["consultant_name"] = editor_info["consultant_name"]

    edit_info["edited_at"] = datetime.now(UTC).isoformat()
    if reason:
        edit_info["reason"] = reason
    return edit_info


async def update_event_message(
    db: AsyncSession,
    event_id: str,
    updated_message: str,
    editor_info: dict | None = None,
    reason: str | None = None,
) -> dict:
    """
    Update event content and preserve original in custom_metadata.

    Creates full audit trail:
    - Preserves original content in Gemini format
    - Records who made the edit
    - Records when the edit was made
    - Records why the edit was made

    Args:
        db: Database session
        event_id: UUID of event to update
        updated_message: New message text
        editor_info: Consultant information (optional)
        reason: Reason for edit (optional)

    Returns:
        dict: Updated event data with audit trail

    Raises:
        ValueError: If event not found
    """

    schema_version = await _detect_events_schema(db)
    if schema_version == _EVENTS_SCHEMA_V1:
        query = text(
            """
            SELECT id, app_name, user_id, session_id, event_data, timestamp
            FROM events
            WHERE id = :event_id
            LIMIT 1
            """
        )
    else:
        query = text(
            """
            SELECT id, app_name, user_id, session_id, content, custom_metadata, timestamp
            FROM events
            WHERE id = :event_id
            LIMIT 1
            """
        )

    result = await db.execute(query, {"event_id": event_id})
    event = result.mappings().first()

    if not event:
        raise ValueError(f"Event {event_id} not found")

    if schema_version == _EVENTS_SCHEMA_V1:
        event_data = dict(event["event_data"] or {})
        original_content = event_data.get("content")
        custom_metadata = event_data.get("custom_metadata") or event_data.get("customMetadata") or {}
    else:
        event_data = None
        original_content = event["content"]
        custom_metadata = event["custom_metadata"] or {}

    original_message = _extract_original_message(original_content)

    new_content = {"role": "model", "parts": [{"text": updated_message}]}
    edit_info = _build_edit_info(editor_info, reason)

    new_custom_metadata = {
        **custom_metadata,
        "edited": True,
        "original_content": original_content,
        "edit_info": edit_info,
    }

    if schema_version == _EVENTS_SCHEMA_V1:
        event_data = event_data or {}
        updated_event_data = {
            **event_data,
            "content": new_content,
            "custom_metadata": new_custom_metadata,
        }
        update_query = text(
            """
            UPDATE events
            SET event_data = CAST(:event_data AS jsonb)
            WHERE id = :event_id
              AND app_name = :app_name
              AND user_id = :user_id
              AND session_id = :session_id
            RETURNING id, session_id, timestamp
            """
        )
        params = {
            "event_id": event_id,
            "app_name": event["app_name"],
            "user_id": event["user_id"],
            "session_id": event["session_id"],
            "event_data": json.dumps(updated_event_data),
        }
    else:
        update_query = text(
            """
            UPDATE events
            SET content = CAST(:new_content AS jsonb),
                custom_metadata = CAST(:new_metadata AS jsonb)
            WHERE id = :event_id
              AND app_name = :app_name
              AND user_id = :user_id
              AND session_id = :session_id
            RETURNING id, session_id, timestamp
            """
        )
        params = {
            "event_id": event_id,
            "app_name": event["app_name"],
            "user_id": event["user_id"],
            "session_id": event["session_id"],
            "new_content": json.dumps(new_content),
            "new_metadata": json.dumps(new_custom_metadata),
        }

    try:
        result = await db.execute(update_query, params)
        updated_event = result.mappings().first()
        if not updated_event:
            raise ValueError(f"Failed to update event {event_id}")
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    logger.info(" Audit Trail Created:")
    logger.info(f"Event ID: {event_id}")
    logger.info(f"Consultant: {edit_info.get('consultant_name', 'Unknown')}")
    logger.info(f"Timestamp: {edit_info['edited_at']}")
    logger.info(f"Reason: {edit_info.get('reason', 'Not provided')}")
    logger.info(f"Original length: {len(original_message)} chars")
    logger.info(f"Updated length: {len(updated_message)} chars")
    logger.info(f"Length diff: {len(updated_message) - len(original_message):+d} chars")

    return {
        "event_id": str(updated_event["id"]),
        "session_id": updated_event["session_id"],
        "original_message": original_message,
        "updated_message": updated_message,
        "updated_at": datetime.now(UTC),
    }
