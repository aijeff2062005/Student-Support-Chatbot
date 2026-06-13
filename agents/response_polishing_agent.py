"""Response Polishing Agent - Natural language response formatter."""

import logging
import traceback

from google.adk.agents import Agent
from google.adk.agents.callback_context import CallbackContext
from google.adk.models.lite_llm import LiteLlm

from agents.data_collector_agent import get_conversation_turns
from configs.llm_client import get_litellm_config
from tools.response_agent_prompt_builder import ResponsePolishingPromptState, build_response_polishing_prompt
from tools.response_agent_prompt_rules import DEFAULT_FOLLOW_UP_HINT_PROMPT

logger = logging.getLogger(__name__)

_cfg = get_litellm_config(task="response_polishing")


def agent_answer_prompt_builder(callback_context: CallbackContext):
    # --- 1. PRE-FETCH HISTORY TO ENSURE FRESH STATE ---
    try:
        # Build turn-based conversation history
        conversation_turns = get_conversation_turns(callback_context, limit=5)
        callback_context.state["conversation_turns"] = conversation_turns
    except Exception as e:
        logger.error(f"Error fetching history in prompt builder: {e}")
        traceback.print_exc()

    # --- 2. BUILD PROMPT WITH FRESH STATE ---
    prompt_state = ResponsePolishingPromptState(
        self_pronoun=callback_context.state.get("self_pronoun", "mình"),
        user_pronoun=callback_context.state.get("user_pronoun", "bạn"),
        user_role=callback_context.state.get("user_state", {}).get("role", None),
        # DATA SOURCES (original variables)
        original_query=callback_context.state.get("original_query", None),
        query_intent=callback_context.state.get("intent", None),
        query_topic=callback_context.state.get("topic", None),
        need_disambig=callback_context.state.get("need_disambig", None),
        query_results=callback_context.state.get("query_results", None),
        data_lv2_data_enriched_nodes=callback_context.state.get("data_lv2_data_enriched_nodes", None),
        data_lv2_siblings_relative=callback_context.state.get("data_lv2_siblings_relative", None),
        data_lv2_siblings_similar=callback_context.state.get("data_lv2_siblings_similar", None),
        user_major=callback_context.state.get("user_major", None),
        user_topic=callback_context.state.get("user_topic", None),
        user_personalization=callback_context.state.get("user_state", {}).get("personalization", None),
        playbook_action_results=callback_context.state.get("playbook_action_results", None),
        confidence_score_action_results=callback_context.state.get("confidence_score_action_results", None),
        content_offerings_text=callback_context.state.get("content_offerings_text", None),
        conversation_turns=callback_context.state.get("conversation_turns", None),
        has_media=callback_context.state.get("has_media", False),
        media_attachments=callback_context.state.get("extra_data", {}).get("attachments", None),
        is_query=callback_context.state.get("is_query", False),
        answer_plan=callback_context.state.get("answer_plan", None),
        data_crawled=callback_context.state.get("data_crawled", None),
        # GUIDANCE (original variables)
        playbook_answer_guidance=callback_context.state.get("playbook_answer_guidance", None),
        confidence_score_answer_guidance=callback_context.state.get("confidence_score_answer_guidance", None),
        # HINT DEEP EXPLORE QUESTION
        question_explore_deep_hint=callback_context.state.get(
            "question_explore_deep_hint",
            DEFAULT_FOLLOW_UP_HINT_PROMPT,
        ),
        playbook_action_instruction=callback_context.state.get("playbook_action_instruction", None),
    )
    callback_context.state["agent_answer_prompt"] = build_response_polishing_prompt(state=prompt_state)


response_polishing_agent = Agent(
    model=LiteLlm(**_cfg),
    name="response_polishing_agent",
    # instruction=POLISHING_PROMPT,
    instruction="{agent_answer_prompt}",
    before_agent_callback=agent_answer_prompt_builder,
    # before_model_callback removed to prevent stale state
    include_contents="none",
    # after_model_callback=build_response_for_agent
)
