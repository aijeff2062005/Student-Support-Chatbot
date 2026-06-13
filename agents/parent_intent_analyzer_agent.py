"""Parent Intent Analyzer Agent.

Runs FIRST in the SequentialAgent pipeline.
Classifies the user's message into UC1 (prospective student / ADM_*),
UC2 (current student / STU_*), SPAM, or UNKNOWN.

Sets global state:
  - is_admission_topic: True (UC1) | False (UC2) — only set when ADM_* or STU_* detected
  - user_context_intent: e.g. "ADM_TUITION", "STU_PROGRAM_CHANGE"
  - uc2_transfer_message: transfer message for UC2 (set once)
"""

import logging

# import litellm
from google.adk.agents.llm_agent import LlmAgent
from google.adk.models.lite_llm import LiteLlm

from callbacks.parent_intent_analyzer import (
    after_parent_intent_callback,
    before_parent_intent_model_callback,
    check_parent_intent_should_run,
)
from configs.llm_client import get_litellm_config
from prompts.parent_intent_analyzer import SYSTEM_PROMPT

# litellm._turn_on_debug()
logger = logging.getLogger(__name__)

_cfg = get_litellm_config(task="classification_strict")

parent_intent_analyzer_agent = LlmAgent(
    model=LiteLlm(**_cfg),
    name="parent_intent_analyzer_agent",
    instruction=SYSTEM_PROMPT,
    output_key="parent_intent_result",
    before_agent_callback=check_parent_intent_should_run,
    before_model_callback=before_parent_intent_model_callback,
    after_agent_callback=after_parent_intent_callback,
    include_contents="none",
)

logger.info(" Parent Intent Analyzer Agent initialized")
