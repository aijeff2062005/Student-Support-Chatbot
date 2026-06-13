import logging
from typing import Any, Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

WebhookPlatform = Literal["web", "zalo", "facebook", "tiktok"]


class WebhookAttachment(BaseModel):
    """Inbound media attachment for image QR support."""

    type: str | None = Field(None, description="Attachment type or MIME type")
    mime_type: str | None = Field(None, description="Image MIME type, e.g. image/png")
    content_type: str | None = Field(None, description="Image MIME type from upstream platform")
    mimetype: str | None = Field(None, description="Image MIME type from upstream platform")
    filename: str | None = Field(None, description="Original filename")
    name: str | None = Field(None, description="Original filename from upstream platform")
    url: str | None = Field(None, description="Public image URL")
    image_url: str | None = Field(None, description="Public image URL")
    file_url: str | None = Field(None, description="Public image URL")
    base64: str | None = Field(None, description="Base64 image content or data URL")
    data: str | None = Field(None, description="Base64 image content or data URL")
    content: str | None = Field(None, description="Base64 image content or data URL")


class WebhookAdmissionRequest(BaseModel):
    """
    Webhook request model with standardized field names.

     STANDARDIZED NAMING:
    - user_id: User identifier (was: full_name)
    - session_id: Session/conversation ID (was: conversation)
    """

    user_id: str = Field(..., description="User identifier (name, ID, etc.)")
    session_id: str = Field(..., description="Session/conversation ID")
    message: str = Field(default="", description="User message/question")
    platform: WebhookPlatform | None = Field(None, description="Source platform: web, zalo, facebook, tiktok")
    customer_info: dict[str, Any] | None = Field({}, description="Customer info")
    sale_profile: dict[str, Any] | None = Field({}, description="Sales profile data")
    attachments: list[WebhookAttachment] | None = Field(None, description="Inbound image attachments")

    class Config:
        json_schema_extra = {
            "example": {
                "user_id": "Nguyen Van A",
                "session_id": "MSG-CONV-12345",
                "message": "Tôi muốn tìm hiểu về ngành Công nghệ thông tin",
                "platform": "web",
                "sale_profile": {"sales_name": "John Doe", "sales_gender": "male"},
                "attachments": [],
            }
        }


class WebhookAdmissionResponse(BaseModel):
    """
    Webhook response model with standardized field names.

     STANDARDIZED NAMING:
    - user_id: User identifier (was: full_name)
    - session_id: Session/conversation ID (was: conversation)
    """

    event_id: str | None = Field(None, description="Event ID")
    status: str = Field(..., description="Processing status: success/error")
    response: str = Field(..., description="Agent complete response text")
    user_id: str = Field(..., description="User identifier")
    session_id: str = Field(..., description="Session/conversation ID")

    # Include extra_data and processing_info
    extra_data: dict[str, Any] | None = Field(None, description="Extra data from session state")
    invocation_id: str | None = Field(None, description="LLM invocation ID for tracing")
    processing_info: dict[str, Any] | None = Field(None, description="Agent processing details")
    state: dict[str, Any] | None = Field(None, description="Processing state")

    class Config:
        json_schema_extra = {
            "example": {
                "status": "success",
                "response": "Xin chào bạn! Tôi hiểu bạn đang quan tâm đến ngành Công nghệ thông tin...",
                "user_id": "Nguyen Van A",
                "session_id": "MSG-CONV-12345",
                "processing_time_ms": 3500,
                "extra_data": {},
                "processing_info": {
                    "agent_type": "multi_agent_admission",
                    "response_length": 456,
                    "session_updates": {},
                },
            }
        }


class WebhookErrorResponse(BaseModel):
    """Webhook error response model with standardized field names."""

    status: str = "error"
    error: str = Field(..., description="Error description")
    user_id: str | None = Field(None, description="User identifier")
    session_id: str | None = Field(None, description="Session ID")

    class Config:
        json_schema_extra = {
            "example": {
                "status": "error",
                "error": "Invalid message format or empty message",
                "user_id": "Nguyen Van A",
                "session_id": "MSG-CONV-12345",
            }
        }


class WebhookAdmissionUpdateStateRequest(BaseModel):
    """
    Webhook request model with standardized field names.

     STANDARDIZED NAMING:
    - user_id: User identifier (was: full_name)
    - session_id: Session/conversation ID (was: conversation)
    """

    user_id: str = Field(..., description="User identifier (name, ID, etc.)")
    session_id: str = Field(..., description="Session/conversation ID")
    # message: str = Field(..., description="User message/question")
    platform: WebhookPlatform | None = Field(None, description="Source platform: web, zalo, facebook, tiktok")
    customer_info: dict[str, Any] | None = Field({}, description="Customer info")
    update_state: dict[str, Any] | None = Field(None, description="State updates for the session")

    class Config:
        json_schema_extra = {
            "example": {
                "user_id": "Nguyen Van A",
                "session_id": "MSG-CONV-12345",
                "message": "Tôi muốn tìm hiểu về ngành Công nghệ thông tin",
                "platform": "facebook",
                "update_state": {"is_pending_approval": True},
            }
        }
