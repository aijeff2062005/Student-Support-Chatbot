from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class EditorInfo(BaseModel):
    """Information about the consultant who edited the message"""

    consultant_id: str | None = Field(None, description="Consultant ID from auth system")
    consultant_name: str | None = Field(None, description="Consultant display name")


class UpdateEventRequest(BaseModel):
    """Request body for updating an event message"""

    updated_message: str = Field(..., min_length=1, max_length=5000, description="Updated message content")
    editor_info: EditorInfo | None = Field(None, description="Information about who made the edit")
    reason: str | None = Field(None, max_length=500, description="Reason for the edit")

    @field_validator("updated_message")
    def validate_not_empty(cls, v):
        """Validate message is not empty after stripping whitespace"""
        if not v.strip():
            raise ValueError("Message cannot be empty")
        return v.strip()


class UpdateEventResponse(BaseModel):
    """Response after successfully updating an event"""

    success: bool = Field(..., description="Whether the update was successful")
    event_id: str = Field(..., description="UUID of the updated event")
    session_id: str = Field(..., description="Session ID containing the event")
    original_message: str = Field(..., description="Original message content before edit")
    updated_message: str = Field(..., description="New message content after edit")
    updated_at: datetime = Field(..., description="Timestamp of the update")
