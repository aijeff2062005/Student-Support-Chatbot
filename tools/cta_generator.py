import json
import logging

import litellm

from configs.llm_client import get_litellm_config
from prompts.data_collector.cta_generator import SYSTEM_PROMPT as CTA_SYSTEM_PROMPT
from schemas.ctas import ButtonResponse

logger = logging.getLogger(__name__)


def cta_generator(context: dict) -> ButtonResponse:
    """
    Generate Call-To-Actions (CTAs) based on conversation context.

    Args:
        context: Dict containing:
            - conversation: Recent user messages for context
            - cta_hint: CTA hints from get_cta_hint service
            - used_ctas: List of already used CTAs
    Returns:
        A dict with buttons list of CTAs.
    """
    response = litellm.completion(
        **get_litellm_config(task="cta_generation"),
        messages=[
            {"role": "system", "content": CTA_SYSTEM_PROMPT},
            {"role": "user", "content": str(context)},
        ],
        reasoning_effort="none",
        response_format=ButtonResponse,
    )

    result = json.loads(response.choices[0].message.content)

    if "buttons" in result and isinstance(result["buttons"], list):
        for btn in result["buttons"]:
            if not btn.get("link") or btn.get("link") == "":
                btn["link"] = None

    return result
