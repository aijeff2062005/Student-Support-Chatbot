import litellm

from configs.config_service import get_settings as _get_settings
from configs.llm_client import get_litellm_config


def llm_web_search(question: str) -> str:
    _s = _get_settings()
    response = litellm.completion(
        **get_litellm_config(_s.litellm_web_search_model, task="web_qa"),
        messages=[
            {"role": "system", "content": "Answer this question shortly and clearly in Vietnamese."},
            {"role": "user", "content": question},
        ],
        reasoning_effort="none",
    )
    return response.choices[0].message.content
