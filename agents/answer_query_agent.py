"""Answer Query Agent - Strict data-only response agent.

Handles: PRIMARY_FACTS, DATA_CRAWLED, ENRICHED_ATTRIBUTES,
SIBLINGS_RELATIVE/SIMILAR, FOLLOW_UP_HINTS, ANSWER_PLAN, CONFIDENCE_SCORE.

Rules: NEVER hallucinate. ONLY answer from provided data blocks.
"""

import logging
import traceback

from google.adk.agents import Agent
from google.adk.agents.callback_context import CallbackContext
from google.adk.models.lite_llm import LiteLlm
from google.genai import types

from agents.data_collector_agent import get_conversation_turns, get_fast_history
from configs.llm_client import get_litellm_config
from prompts.qr_error_answer import build_qr_error_answer_prompt
from services.qr_support import QR_ATTACHMENT_NOOP_STATE_KEY, QR_ERROR_CONTEXT_STATE_KEY, QR_ERROR_FAST_PATH_STATE_KEY
from tools.response_agent_prompt_builder import AnswerQueryPromptState, build_answer_query_prompt
from tools.response_agent_prompt_rules import DEFAULT_FOLLOW_UP_HINT_PROMPT

logger = logging.getLogger(__name__)

_cfg = get_litellm_config(task="factual_answering")


def answer_query_prompt_builder(callback_context: CallbackContext):
    """Build prompt for answer_query_agent. Returns empty Content if no data."""
    callback_context.state["answer_query_prompt"] = None

    # --- 0. SKIP if user is blocked ---
    is_blocked = callback_context.state.get("is_blocked", False)
    if is_blocked:
        logger.info("[answer_query_agent] is_blocked=True -> skipping (user blocked).")
        return types.Content(parts=[types.Part(text="")], role="model")

    if callback_context.state.get(QR_ATTACHMENT_NOOP_STATE_KEY):
        logger.info("[answer_query_agent] Non-QR attachment without text -> skipping answer.")
        callback_context.state["is_query"] = False
        callback_context.state["answer_query_prompt"] = None
        callback_context.state["extra_data"] = {"attachments": None}
        return types.Content(parts=[types.Part(text="")], role="model")

    if callback_context.state.get(QR_ERROR_FAST_PATH_STATE_KEY):
        qr_error_context = callback_context.state.get(QR_ERROR_CONTEXT_STATE_KEY)
        if isinstance(qr_error_context, dict) and qr_error_context:
            callback_context.state["is_query"] = True
            callback_context.state["skip_playbook_scenario"] = True
            callback_context.state["extra_data"] = {"attachments": None}
            callback_context.state["answer_query_prompt"] = build_qr_error_answer_prompt(
                qr_error_context=qr_error_context,
                self_pronoun=callback_context.state.get("self_pronoun", "mình"),
                user_pronoun=callback_context.state.get("user_pronoun", "bạn"),
            )
            logger.info("[answer_query_agent] QR fast-path prompt built.")
            return None

        logger.warning("[answer_query_agent] QR fast-path set but context is missing; skipping.")
        return types.Content(parts=[types.Part(text="")], role="model")

    crm_update_state = callback_context.state.get("crm_update_state", False)

    if crm_update_state:
        logger.info("[answer_query_agent] skipped by before_agent_callback due to CRM update state..")
        return types.Content(
            parts=[
                types.Part(text="Agent answer_query_agent skipped by before_agent_callback due to CRM update state.")
            ],
            role="model",
        )

    uc = callback_context.state.get("uc", {})
    if uc == "UC3" or uc == "UC4":
        logger.info("[AdmissionTopicSkip] skipping agent.")
        return types.Content(
            parts=[types.Part(text=f"[answer_query_agent] skipped — user is {uc}")],
            role="model",
        )

    # --- 0a. SKIP if UC2 user (is_admission_topic=False) ---
    is_admission_topic = callback_context.state.get("is_admission_topic", None)
    # UC2 skip disabled: let current-student/support questions be answered from data.
    if False and is_admission_topic is False:
        logger.info("[answer_query_agent] is_admission_topic=False -> skipping (UC2 user).")
        return types.Content(parts=[types.Part(text="")], role="model")

    # --- 0b. SKIP if spam detected ---
    is_spam = callback_context.state.get("user_state", {}).get("is_spam", False)
    if is_spam:
        logger.info("[answer_query_agent] is_spam=True -> skipping (spam).")
        return types.Content(parts=[types.Part(text="")], role="model")

    current_is_query = callback_context.state.get("is_query", False)
    if not current_is_query:
        logger.info("[answer_query_agent] skip_playbook_scenario=True -> skipping (nurturing workflow).")
        return types.Content(parts=[types.Part(text="")], role="model")

    # --- 1. PRE-FETCH HISTORY ---
    try:
        state = callback_context.state.to_dict()

        # Build turn-based conversation history
        conversation_turns = get_conversation_turns(callback_context, limit=5)
        callback_context.state["conversation_turns"] = conversation_turns

        qna_mode = state.get("qna_mode", False)
        if qna_mode:
            # Get latest user input from last turn
            if conversation_turns:
                callback_context.state["latest_user_input"] = conversation_turns[-1].get("user_message", "")
            else:
                latest_user = get_fast_history(callback_context, limit=1, author_filter="user")
                callback_context.state["latest_user_input"] = latest_user[0] if latest_user else ""

    except Exception as e:
        logger.error(f"Error fetching history in answer_query_prompt_builder: {e}")
        traceback.print_exc()

    # --- 2. BUILD PROMPT ---
    is_query = callback_context.state.get("is_query", False)
    if is_query:
        prompt_state = AnswerQueryPromptState(
            self_pronoun=callback_context.state.get("self_pronoun", "mình"),
            user_pronoun=callback_context.state.get("user_pronoun", "bạn"),
            user_role=callback_context.state.get("user_state", {}).get("role", None),
            # DATA SOURCES
            original_query=callback_context.state.get("original_query", None),
            query_intent=callback_context.state.get("intent", None),
            query_topic=callback_context.state.get("topic", None),
            need_disambig=callback_context.state.get("need_disambig", None),
            query_results=callback_context.state.get("query_results", None),
            data_lv2_data_enriched_nodes=callback_context.state.get("data_lv2_data_enriched_nodes", None),
            data_lv2_siblings_relative=callback_context.state.get("data_lv2_siblings_relative", None),
            data_lv2_siblings_similar=callback_context.state.get("data_lv2_siblings_similar", None),
            data_lv2_data_enriched_relation=callback_context.state.get("data_lv2_data_enriched_relation", None),
            data_lv2_related_nodes=callback_context.state.get("data_lv2_related_nodes", None),
            user_major=callback_context.state.get("user_major", None),
            user_topic=callback_context.state.get("user_topic", None),
            confidence_score_action_results=callback_context.state.get("confidence_score_action_results", None),
            conversation_turns=callback_context.state.get("conversation_turns", None),
            has_media=callback_context.state.get("has_media", False),
            media_attachments=callback_context.state.get("extra_data", {}).get("attachments", None),
            is_query=callback_context.state.get("is_query", False),
            answer_plan=callback_context.state.get("answer_plan", None),
            data_crawled=callback_context.state.get("data_crawled", None),
            multi_query_pairs=callback_context.state.get("multi_query_pairs", None),
            is_mixed_query=callback_context.state.get("is_mixed_query", False),
            is_spam=callback_context.state.get("user_state", {}).get("is_spam", False),
            # FOLLOW-UP HINTS
            question_explore_deep_hint=callback_context.state.get(
                "question_explore_deep_hint",
                DEFAULT_FOLLOW_UP_HINT_PROMPT,
            ),
        )

        prompt = build_answer_query_prompt(state=prompt_state, callback_context=callback_context)

        # if not prompt:

        callback_context.state["answer_query_prompt"] = prompt
    else:
        callback_context.state["answer_query_prompt"] = None
        return types.Content(parts=[types.Part(text="")], role="model")
    return None


answer_query_agent = Agent(
    model=LiteLlm(**_cfg),
    name="answer_query_agent",
    instruction="{answer_query_prompt}",
    before_agent_callback=answer_query_prompt_builder,
    include_contents="none",
)
