import logging
import traceback

from fastapi import APIRouter, HTTPException

from crud.update_agent_history import update_event_message
from dbs.postgres_history_chat import close_db_session, get_db_session
from schemas.update_agent_history import UpdateEventRequest, UpdateEventResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/update_agent_history", tags=["Update Agent Chat History"])


@router.patch(
    "/events/{event_id}",
    response_model=UpdateEventResponse,
    summary="Update assistant message by event ID",
    description=(
        "Update the content of an assistant message in the events table. "
        "Original content is preserved in custom_metadata for audit trail. "
        "Only allows one edit per event to maintain data integrity."
    ),
    responses={
        200: {"description": "Event successfully updated", "model": UpdateEventResponse},
        400: {"description": "Invalid request (empty message, validation error)"},
        404: {"description": "Event not found"},
        500: {"description": "Internal server error during update"},
    },
)
async def update_event(event_id: str, request: UpdateEventRequest):
    """
    Update assistant message content by event ID.

    **Workflow:**
    1. Validate event exists and hasn't been edited
    2. Extract original message content
    3. Update message in Gemini format
    4. Save original content to custom_metadata
    5. Record audit trail (who, when, why)

    **Parameters:**
    - **event_id**: UUID of the event to update
    - **updated_message**: New message content (1-5000 chars)
    - **editor_info**: Consultant information (optional)
            - consultant_id: ID from auth system
            - consultant_name: Display name
    - **reason**: Reason for the edit (optional, max 500 chars)

    **Returns:**
    Updated event with:
    - Original message (before edit)
    - Updated message (after edit)
    - Audit trail metadata

    **Example Request:**
    ```json
    {
      "updated_message": "Tuition for the IT major at GDU is 15 million VND per year...",
      "editor_info": {
            "consultant_id": "consultant_123",
            "consultant_name": "Consultant A"
      },
      "reason": "Added more details about scholarships"
    }
    ```

    **Example Response:**
    ```json
    {
      "success": true,
      "event_id": "d7664058-cbd1-43f5-a948-65e3aca8372a",
      "session_id": "6951f64b7d0d1cc4922d81c6",
      "original_message": "Tuition for IT is 15 million VND/year",
      "updated_message": "Tuition for the IT major at GDU is 15 million VND per year...",
      "updated_at": "2025-12-29T04:15:30.123456Z"
    }
    ```

    **Error Cases:**
    - 404: Event not found in database
    - 409: Event has already been edited (only one edit allowed)
    - 400: Empty message or validation error
    """

    db = None
    try:
        # Create database session
        db = await get_db_session()

        # Prepare editor info dict
        editor_info_dict = request.editor_info.model_dump(exclude_none=True) if request.editor_info else None

        # Update event with audit trail (sync call, no await)
        result = await update_event_message(
            db=db,
            event_id=event_id,
            updated_message=request.updated_message,
            editor_info=editor_info_dict,
            reason=request.reason,
        )

        logger.info(f"Event {event_id} updated successfully")
        return UpdateEventResponse(success=True, **result)

    except ValueError as e:
        traceback.print_exc()
        # Event not found or already edited
        error_msg = str(e)

        if "not found" in error_msg:
            logger.warning(f" Event not found: {event_id}")
            raise HTTPException(status_code=404, detail=f"Event {event_id} not found") from e
        logger.error(f"Validation error: {error_msg}")
        raise HTTPException(status_code=400, detail=error_msg) from e

    except Exception as e:
        logger.exception(f"Update event error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to update event: {str(e)}") from e
    finally:
        if db is not None:
            await close_db_session(db)
