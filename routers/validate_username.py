import traceback

import litellm
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from configs.config_service import get_settings as _get_settings
from configs.llm_client import get_litellm_config
from prompts.validate_username import MODERATION_SYSTEM_PROMPT

router = APIRouter(prefix="/api/v1", tags=["Update Agent Chat History"])


class UsernameRequest(BaseModel):
    username: str


class ModerationResponse(BaseModel):
    is_valid_username: bool
    reason: str


async def moderate_username_service(username: str) -> ModerationResponse:
    """
    Core service that validates a username against moderation rules.

    This function is reusable and can be imported by background jobs,
    scheduled tasks, or other services.
    """
    prompt = f'Input: "{username}"'

    _s = _get_settings()
    _cfg = get_litellm_config(_s.litellm_username_moderation_model, task="classification_strict")
    _cfg["extra_body"] = {
        "safety_settings": [
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        ]
    }
    response = await litellm.acompletion(
        messages=[
            {"role": "system", "content": MODERATION_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        reasoning_effort="none",
        num_retries=2,
        response_format=ModerationResponse,
        **_cfg,
    )
    content = response.choices[0].message.content
    # If model/proxy safety filter blocked the response, treat username as invalid
    # rather than surfacing a 500. This handles profanity/slur inputs gracefully.
    if not content:
        return ModerationResponse(
            is_valid_username=False,
            reason="Tên người dùng không hợp lệ hoặc vi phạm chính sách nội dung.",
        )
    return ModerationResponse.model_validate_json(content)


@router.post("/validate_username")
async def check_username(request: UsernameRequest):
    try:
        result = await moderate_username_service(request.username)
        return {
            "message": "Check username valid done",
            "data": result,
        }

    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Lỗi hệ thống GenAI") from e
