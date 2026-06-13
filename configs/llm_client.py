"""Centralized LLM Client Configuration — LiteLLM proxy only.

All LLM calls route through the LiteLLM proxy via litellm.use_litellm_proxy.
Proxy URL/key are read from environment variables:
LITELLM_PROXY_API_BASE and LITELLM_PROXY_API_KEY.
"""

from __future__ import annotations

import logging
from typing import Any

import litellm

from configs.config_service import get_settings

# Route ALL litellm calls (chat/embed/rerank) through the LiteLLM proxy.
# Reads LITELLM_PROXY_API_BASE and LITELLM_PROXY_API_KEY from env automatically.
litellm.use_litellm_proxy = True

logger = logging.getLogger(__name__)

_TASK_PROFILES: dict[str, dict[str, Any]] = {
    # Deterministic routing/classification tasks with structured output.
    "classification_strict": {
        "temperature": 0.0,
        "max_tokens": 4096,
    },
    # JSON extraction tasks can keep a tiny amount of flexibility.
    "json_extraction": {
        "temperature": 0.1,
        "max_tokens": 4096,
    },
    # Rewrite to canonical form should be deterministic.
    "rewrite_normalization": {
        "temperature": 0.0,
        "max_tokens": 4096,
    },
    # Tool routing/query analysis should minimize randomness.
    "routing_tool_call": {
        "temperature": 0.0,
        "max_tokens": 4096,
    },
    # Pure factual answer generation from provided data.
    "factual_answering": {
        "temperature": 0.2,
        "max_tokens": 4096,
    },
    # Final wording polish while preserving factuality.
    "response_polishing": {
        "temperature": 0.4,
        "max_tokens": 4096,
    },
    # Persona/nurturing responses with controlled creativity.
    "creative_counseling": {
        "temperature": 0.7,
        "max_tokens": 4096,
    },
    # Web answer synthesis/summarization.
    "web_summary": {
        "temperature": 0.2,
        "max_tokens": 4096,
    },
    # Short direct web QA answer.
    "web_qa": {
        "temperature": 0.3,
        "max_tokens": 4096,
    },
    # Gate/validator callback should be extremely short and deterministic.
    "gatekeeper": {
        "temperature": 0.0,
        "max_tokens": 4096,
    },
    # CTA generation: mostly deterministic but still natural.
    "cta_generation": {
        "temperature": 0.2,
        "max_tokens": 4096,
    },
}


def get_litellm_config(model: str | None = None, task: str = "classification_strict") -> dict[str, Any]:
    """Build LiteLLM config for ADK agents and direct litellm.completion calls.

    The model name should be the public proxy model name
    (for example: gemini-2.5-flash).
    The proxy maps it to the internal provider model automatically
    (for example: deepinfra/google/gemini-2.5-flash).

    Args:
        model: Optional model override. If None, uses `litellm_agent_model` from env.
        task: Named hyperparameter profile by business task.
    """
    s = get_settings()
    profile = _TASK_PROFILES.get(task, _TASK_PROFILES["classification_strict"])
    if task not in _TASK_PROFILES:
        logger.warning("Unknown LLM task profile '%s'. Falling back to classification_strict.", task)

    cfg = {
        "model": model or s.litellm_agent_model,
        "extra_body": {
            "reasoning_effort": "none",
            "allowed_openai_params": ["reasoning_effort"],
        },
    }
    cfg.update(profile)
    return cfg


def get_rerank_params(model: str | None = None) -> dict[str, Any]:
    """Build parameters for litellm.rerank() / litellm.arerank()."""
    s = get_settings()
    return {
        "model": f"{model or s.reranker_model}",
        "api_key": s.litellm_proxy_api_key,
        "api_base": s.litellm_proxy_api_base,
    }


def get_embedding_params(model: str | None = None) -> dict[str, Any]:
    """Build parameters for litellm.embedding() / litellm.aembedding()."""
    s = get_settings()
    return {
        "model": f"{model or s.embedding_model}",
        "api_key": s.litellm_proxy_api_key,
        "api_base": f"{s.litellm_proxy_api_base}/v1",
        "encoding_format": "float",
        "dimensions": s.embedding_dimension,
    }
