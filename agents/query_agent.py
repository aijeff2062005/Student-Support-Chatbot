"""EDU Agent - Intelligent question routing using MCP-only architecture.

 KEY PRINCIPLE: This agent ONLY communicates via MCP tools.
No direct database imports. All DB access goes through MCP Server.

Architecture:
  Agent → MCP RPC calls → MCP Server → Services → Databases
"""

import traceback

from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.llm_agent import LlmAgent
from google.adk.models import LlmRequest, LlmResponse
from google.adk.models.lite_llm import LiteLlm
from google.adk.tools import ToolContext
from google.genai import types

from callbacks.confidence_score_dmn_callback import after_query_agent_callback, execute_query_tool_callback
from callbacks.parent_intent_analyzer import check_admission_topic_skip
from configs.config_service import get_settings
from configs.llm_client import get_litellm_config
from prompts.query_agent import SYSTEM_PROMPT as QUERY_AGENT_PROMPT
from prompts.query_agent_xml_ver import SYSTEM_PROMPT as QUERY_AGENT_XML_PROMPT
from schemas.query_agents import QueryAgentOutputList
from services.qr_support import QR_ATTACHMENT_NOOP_STATE_KEY, QR_ERROR_FAST_PATH_STATE_KEY
from utils.logging_config import get_logger

logger = get_logger(__name__)

settings = get_settings()

# Configure LLM and MCP
mcp_host = settings.mcp_host
mcp_port = settings.mcp_port
mcp_url = f"http://{mcp_host}:{mcp_port}/mcp"
logger.info("=" * 70)
logger.info(" EDU Agent - MCP-Only Architecture")
logger.info("=" * 70)
logger.info(f"MCP Server: {mcp_url}")
query_agent_prompt_style = (settings.query_agent_prompt_style or "old").strip().lower()
if query_agent_prompt_style not in {"old", "new"}:
    logger.warning(
        "Invalid QUERY_AGENT_PROMPT_STYLE=%s. Falling back to 'old'.",
        settings.query_agent_prompt_style,
    )
    query_agent_prompt_style = "old"
selected_query_agent_prompt = QUERY_AGENT_XML_PROMPT if query_agent_prompt_style == "new" else QUERY_AGENT_PROMPT
logger.info("Query Agent prompt style: %s", query_agent_prompt_style)
logger.info("=" * 70)


def reset_query_state_keys(callback_context: CallbackContext):
    """Reset all query-related state keys to None."""
    logger.info("Resetting query-related state keys (is_query=False detected in LLM response)")

    # Set is_query flag to False
    callback_context.state["is_query"] = False

    # Core query results
    callback_context.state["query_results"] = None
    callback_context.state["confidence_score"] = None
    callback_context.state["has_media"] = None
    callback_context.state["is_required_media"] = None

    # Confidence score action keys
    callback_context.state["confidence_score_action_results"] = None
    callback_context.state["confidence_score_answer_guidance"] = None
    callback_context.state["confidence_score_action_answer_guidance"] = None

    # Data lv2 reasoning keys
    callback_context.state["data_lv2_data_enriched_nodes"] = None
    callback_context.state["data_lv2_siblings_relative"] = None
    callback_context.state["data_lv2_siblings_similar"] = None

    # Reset attachments in extra_data
    if "extra_data" in callback_context.state:
        try:
            current_extra_data = callback_context.state["extra_data"]
            if isinstance(current_extra_data, dict):
                current_extra_data["attachments"] = None
                callback_context.state["extra_data"] = current_extra_data
        except Exception:
            pass

    logger.info("All query-related state keys reset to None")


def query_agent_after_model_callback(
    callback_context: CallbackContext, llm_response: LlmResponse
) -> LlmResponse | None:
    """
    After model callback for Query Agent.
    Checks if LLM decided is_query=False and resets state keys accordingly.

    This runs AFTER LLM generates response but BEFORE tool execution.
    """
    if not llm_response or not llm_response.content:
        return None

    # Check if there's a function call in the response
    has_query_tool_call = False
    is_query_value = None

    response_parts = getattr(getattr(llm_response, "content", None), "parts", None)
    if not isinstance(response_parts, list):
        return llm_response

    for part in response_parts:
        if hasattr(part, "function_call") and part.function_call:
            func_call = part.function_call
            if func_call.name == "query_internal_data":
                has_query_tool_call = True
                # Extract is_query from function call arguments
                if hasattr(func_call, "args") and func_call.args:
                    output_args = func_call.args.get("output", {})
                    # If output is a dictionary (common when parsed from JSON), get is_query
                    if isinstance(output_args, dict):
                        is_query_value = output_args.get("is_query", None)
                    # If it's already an object (less likely at this stage depending on parser), try getattr
                    else:
                        is_query_value = getattr(output_args, "is_query", None)

                    logger.debug("Detected query_internal_data call with is_query=%s", is_query_value)
                break

    # If no tool call OR is_query=False, reset state
    if not has_query_tool_call:
        logger.warning("No query_internal_data tool call detected - resetting state")
        reset_query_state_keys(callback_context)
    elif is_query_value is False:
        logger.warning("is_query=False detected in tool call - resetting state")
        reset_query_state_keys(callback_context)

    # Return None to proceed normally (don't modify the response)
    return None


def query_agent_before_model_callback(
    callback_context: ToolContext | CallbackContext, llm_request: LlmRequest
) -> LlmRequest | None:
    try:
        tool_context = callback_context
        if not tool_context or not llm_request.contents:
            return None

        rewrite_segments = tool_context.state.get("query_rewrite_segments") or []
        rewrite_segment_count = tool_context.state.get("query_rewrite_segment_count", 0)

        if not isinstance(rewrite_segments, list) or not rewrite_segments:
            return None

        last_content = llm_request.contents[-1]
        if not last_content.parts:
            return None

        last_part = last_content.parts[-1]
        if not hasattr(last_part, "text") or not last_part.text:
            return None

        original_text = str(last_part.text)
        contract_header = "Rewrite Segment Contract:"
        if contract_header in original_text:
            return None

        query_lines = "\n".join(f"{idx}. {segment}" for idx, segment in enumerate(rewrite_segments, start=1))
        contract = (
            f"{contract_header}\n"
            f"- Expected query segment count: {rewrite_segment_count}\n"
            "- Treat each query segment below as one independent sub-query unit.\n"
            "- Preserve the exact segment order in the output array.\n"
            "- Do not merge different segments back into one output item just because they are all WHAT.\n"
            f"Query segments:\n{query_lines}\n\n"
        )
        last_part.text = contract + "Current Input: " + original_text
        logger.info("Injected rewrite segment contract into Query Agent prompt")
        return None

    except Exception as e:
        logger.error(f"Error in query_agent_before_model_callback: {e}")
        traceback.print_exc()

    return None


def query_agent_before_agent_callback(callback_context: CallbackContext) -> types.Content | None:
    if callback_context.state.get(QR_ATTACHMENT_NOOP_STATE_KEY):
        reset_query_state_keys(callback_context)
        callback_context.state["is_query"] = False
        callback_context.state["query_agent_output"] = []
        callback_context.state["query_agent_segment_mismatch"] = None
        logger.info("[QA_agent] Non-QR attachment without text -> skipping query agent.")
        return types.Content(
            parts=[types.Part(text="Agent QA_agent skipped — non-QR attachment without text.")],
            role="model",
        )

    if callback_context.state.get(QR_ERROR_FAST_PATH_STATE_KEY):
        reset_query_state_keys(callback_context)
        callback_context.state["is_query"] = True
        callback_context.state["query_agent_output"] = []
        callback_context.state["query_agent_segment_mismatch"] = None
        logger.info("[QA_agent] QR fast-path detected -> skipping query agent and query plan.")
        return types.Content(
            parts=[types.Part(text="Agent QA_agent skipped by QR fast-path.")],
            role="model",
        )

    skip_content = check_admission_topic_skip(callback_context)
    if skip_content is not None:
        return skip_content

    rewrite_results = callback_context.state.get("normalized_message_rewrite_result")
    if not isinstance(rewrite_results, list):
        return None

    query_segments = callback_context.state.get("query_rewrite_segments")
    has_query_segment = isinstance(query_segments, list) and any(
        isinstance(segment, str) and segment.strip() for segment in query_segments
    )
    if has_query_segment:
        return None

    reset_query_state_keys(callback_context)
    callback_context.state["query_agent_output"] = []
    callback_context.state["query_agent_segment_mismatch"] = None
    logger.info("[QA_agent] Message rewrite produced no query segments -> skipping Query Agent.")
    return types.Content(
        parts=[types.Part(text="Agent QA_agent skipped — message_rewrite produced no query segments.")],
        role="model",
    )


def combined_query_agent_before_model_callback(
    callback_context: CallbackContext, llm_request: LlmRequest
) -> LlmRequest | None:
    """
    Combines context injection and logging callbacks.
    """
    # 1. Inject context
    updated_request = query_agent_before_model_callback(callback_context, llm_request)

    # Use updated request if available, otherwise original

    # 2. Log request
    # log_llm_request(callback_context, req_to_log)

    return updated_request


_cfg = get_litellm_config(task="routing_tool_call")

query_analyzer_agent = LlmAgent(
    model=LiteLlm(**_cfg),
    name="QA_agent",
    instruction=selected_query_agent_prompt,
    # after_tool_callback=log_output_for_tool,
    before_agent_callback=query_agent_before_agent_callback,
    before_model_callback=query_agent_before_model_callback,
    after_model_callback=execute_query_tool_callback,
    after_agent_callback=after_query_agent_callback,
    include_contents="none",
    output_schema=QueryAgentOutputList,
    output_key="query_agent_output",
)
