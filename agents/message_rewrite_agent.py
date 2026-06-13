"""Message Rewrite Agent.

Runs immediately after ParentIntentAnalyzer.
Rewrites implicit user inputs into a normalized JSON array of question items before
passing a single downstream-safe text message to the parallel Query and Data Collector agents.
"""

import logging

from google.adk.agents.llm_agent import LlmAgent
from google.adk.models import LiteLlm

from callbacks.message_rewrite import (
    after_rewrite_agent_callback,
    before_rewrite_agent_callback,
    before_rewrite_model_callback,
)
from configs.llm_client import get_litellm_config
from prompts.message_rewrite import SYSTEM_PROMPT

logger = logging.getLogger(__name__)

_cfg = get_litellm_config(task="rewrite_normalization")

message_rewrite_agent = LlmAgent(
    model=LiteLlm(**_cfg),
    name="message_rewrite_agent",
    instruction=SYSTEM_PROMPT,
    output_key="message_rewrite_result",
    before_agent_callback=before_rewrite_agent_callback,
    before_model_callback=before_rewrite_model_callback,
    after_agent_callback=after_rewrite_agent_callback,
    include_contents="none",
)

logger.info(" Message Rewrite Agent initialized with JSON array rewrite contract")
