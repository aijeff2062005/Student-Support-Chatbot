import asyncio
import concurrent.futures
import logging
import traceback

import nest_asyncio
from google.adk.agents.callback_context import CallbackContext
from google.adk.models import LlmResponse
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from schemas.query_agents import QueryAgentOutputSchemaV2
from tools.query_agent import query_internal_data
from tools.why_query_plan_dmn_processing import QueryPlanDMNProcessor
from utils.json_utils import extract_json

logger = logging.getLogger(__name__)


# tool_context.actions.transfer_to_agent
def transfer_to_qna_agent(tool_context: ToolContext, type_hint: str):
    """
    A callback that always performs TransferToAgent to the parent Agent.
    """

    # current_agent = callback_context._invocation_context.agent
    tool_context.state.to_dict()

    override_content = types.Content(
        parts=[types.Part(function_call=types.FunctionCall(name="transfer_to_agent", args={"agent_name": "QA_agent"}))]
    )
    return override_content


# Helpers


def _reset_query_state_full(callback_context: CallbackContext) -> None:
    """
    Full reset of all query-related state keys.
    Called once before MIXED mode parallel execution so each sub-query starts clean.
    """
    keys_to_reset = [
        "query_results",
        "confidence_score",
        "has_media",
        "is_required_media",
        "answer_plan",
        "primary_entities",
        "context_entities",
        "compare_targets",
        "potential_entities",
        "subtopics",
        "time_filter",
        "original_query",
        "data_crawled",
        "confidence_score_action_results",
        "confidence_score_answer_guidance",
        "confidence_score_action_answer_guidance",
        "data_lv2_data_enriched_nodes",
        "data_lv2_siblings_relative",
        "data_lv2_siblings_similar",
        "data_lv2_data_enriched_relation",
        "data_lv2_related_nodes",
        "qtype",
        "intent",
        "topic",
        "compare_mode",
        "time_compare",
        "need_disambig",
        "question_type",
        "entity",
        "multi_query_pairs",
        "is_mixed_query",
    ]
    for key in keys_to_reset:
        callback_context.state[key] = None

    # Reset attachments inside extra_data
    if "extra_data" in callback_context.state:
        try:
            existing = callback_context.state["extra_data"]
            if isinstance(existing, dict):
                existing["attachments"] = None
                callback_context.state["extra_data"] = existing
        except Exception:
            pass
    logger.info(" [Callback] Full state reset complete")


def _process_single_sub_query(
    item: QueryAgentOutputSchemaV2,
    base_state_snapshot: dict,
) -> dict:
    """
    Process one sub-query from a MIXED array.
    Runs in its own thread. Uses an isolated override_state dict.
    Does NOT touch the shared tool_context.state.
    """
    # Build isolated state snapshot for this sub-query
    override_state = dict(base_state_snapshot)  # shallow copy of shared base
    override_state["qtype"] = item.question_type
    override_state["intent"] = item.intent
    override_state["topic"] = item.primary_topic
    override_state["subtopics"] = item.subtopics or []
    override_state["compare_mode"] = item.compare_mode
    override_state["time_compare"] = item.time_compare.model_dump() if hasattr(item.time_compare, "model_dump") else item.time_compare
    override_state["needs_disambiguation"] = item.needs_disambiguation
    override_state["primary_entities"] = [
        e.model_dump() if hasattr(e, "model_dump") else dict(e) for e in (item.primary_entities or [])
    ]
    override_state["compare_targets"] = [
        e.model_dump() if hasattr(e, "model_dump") else dict(e) for e in (item.compare_targets or [])
    ]
    override_state["context_entities"] = [
        e.model_dump() if hasattr(e, "model_dump") else dict(e) for e in (item.context_entities or [])
    ]
    override_state["potential_entities"] = [
        e.model_dump() if hasattr(e, "model_dump") else dict(e) for e in (item.potential_entities or [])
    ]
    override_state["original_query"] = item.original_query
    override_state["input_data"] = item.model_dump()
    override_state["time_filter"] = item.time if item.time else None

    processor = QueryPlanDMNProcessor()
    return processor.process_query_plan_with_override(override_state)


def _run_sub_queries_parallel(
    items: list[QueryAgentOutputSchemaV2],
    base_state_snapshot: dict,
) -> list[dict]:
    """
    Run all sub-queries in parallel threads using ThreadPoolExecutor + nest_asyncio.
    Returns an ordered list of result dicts.
    """
    # Apply nest_asyncio so asyncio.run() works inside an already-running loop
    try:
        nest_asyncio.apply()
    except Exception:
        pass

    async def _gather():
        loop = asyncio.get_event_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(items)) as executor:
            futures = [
                loop.run_in_executor(
                    executor,
                    _process_single_sub_query,
                    item,
                    base_state_snapshot,
                )
                for item in items
            ]
            return await asyncio.gather(*futures)

    try:
        results = asyncio.run(_gather())
    except RuntimeError:
        loop = asyncio.get_event_loop()
        results = loop.run_until_complete(_gather())

    return list(results)


# Main callbacks


def execute_query_tool_callback(callback_context: CallbackContext, llm_response: LlmResponse) -> LlmResponse:
    """
    After-model callback that parses the LLM JSON array response,
    validates each element against QueryAgentOutputSchemaV2, and
    executes query processing:
    - 1 item  → single mode (calls query_internal_data as before)
    - 2+ items → MIXED mode (parallel threads, stores multi_query_pairs)

    """
    try:
        callback_context.state["query_agent_segment_mismatch"] = None
        # logger.error(f"[Callback] execute_query_tool_callback triggered with LLM response: {llm_response}")
        response_content = getattr(llm_response, "content", None)
        response_parts = getattr(response_content, "parts", None)
        if (
            llm_response is not None
            and getattr(llm_response, "partial", None) in (False, None)
            and isinstance(response_parts, list)
            and response_parts
        ):
            content = response_parts[0].text
            json_content = extract_json(content)

            # Handle different response formats:
            if isinstance(json_content, dict):
                if "items" in json_content:
                    # QueryAgentOutputList wrapper format
                    json_content = json_content["items"]
                else:
                    json_content = [json_content]

            items: list[QueryAgentOutputSchemaV2] = [
                QueryAgentOutputSchemaV2(**obj) for obj in json_content if obj.get("is_query") is True
            ]
            callback_context.state["query_agent_output"] = [item.model_dump(exclude_none=True) for item in items]

            expected_segment_count = callback_context.state.get("query_rewrite_segment_count", 0)
            rewrite_segments = callback_context.state.get("query_rewrite_segments") or []
            if expected_segment_count and len(items) != expected_segment_count:
                mismatch = {
                    "expected": expected_segment_count,
                    "actual": len(items),
                    "segments": rewrite_segments,
                }
                callback_context.state["query_agent_segment_mismatch"] = mismatch
                logger.warning(
                    "[Callback] Query segment mismatch detected: expected=%s actual=%s segments=%s",
                    expected_segment_count,
                    len(items),
                    rewrite_segments,
                )

            if not items:
                logger.warning("[Callback] LLM returned empty array  treating as not_query")
                callback_context.state["is_query"] = False
                return llm_response

            logger.info(f"[Callback] Parsed {len(items)} sub-queries from LLM")

            any_is_query = any(item.is_query for item in items)
            if not any_is_query:
                callback_context.state["is_query"] = False
                return llm_response

            if len(items) == 1:
                logger.info("[Callback] Single sub-query  single mode")
                callback_context.state["is_mixed_query"] = False
                try:
                    pair = query_internal_data(
                        tool_context=callback_context,
                        input_tool=items[0],
                        return_result=True,
                    )
                    # Store as a 1-element pairs list (unified path)
                    if pair is None:
                        pair = {}
                    # Fill mandatory keys that query_internal_data may not set
                    pair.setdefault("original_query", items[0].original_query)
                    pair.setdefault("question_type", items[0].question_type)
                    pair.setdefault("answer_plan", callback_context.state.get("answer_plan"))
                    pair.setdefault("data_crawled", callback_context.state.get("data_crawled"))
                    pair.setdefault(
                        "data_lv2_data_enriched_nodes", callback_context.state.get("data_lv2_data_enriched_nodes")
                    )
                    pair.setdefault(
                        "data_lv2_siblings_relative", callback_context.state.get("data_lv2_siblings_relative")
                    )
                    pair.setdefault(
                        "data_lv2_siblings_similar", callback_context.state.get("data_lv2_siblings_similar")
                    )
                    pair.setdefault(
                        "data_lv2_data_enriched_relation", callback_context.state.get("data_lv2_data_enriched_relation")
                    )
                    pair.setdefault("data_lv2_related_nodes", callback_context.state.get("data_lv2_related_nodes"))
                    callback_context.state["multi_query_pairs"] = [pair]
                    callback_context.state["is_query"] = True
                    logger.info(f"[Callback] Single mode pairs stored: original_query={pair.get('original_query')}")
                except Exception as e:
                    logger.error(f"[Callback] Error in single mode: {e}")
                    traceback.print_exc()

            else:
                logger.info(f"[Callback] MIXED mode  processing {len(items)} sub-queries in parallel")
                callback_context.state["is_mixed_query"] = True
                callback_context.state["is_query"] = True

                # Full reset once before spawning threads
                _reset_query_state_full(callback_context)
                callback_context.state["is_mixed_query"] = True
                callback_context.state["is_query"] = True

                # Snapshot of shared state fields each sub-query may need
                base_snapshot = {
                    k: callback_context.state.get(k)
                    for k in (
                        "personalization",
                        "user_name",
                        "user_pronoun",
                        "user_major",
                        "conversation_history_summary",
                        "session_id",
                        "language",
                    )
                }

                pairs = _run_sub_queries_parallel(items, base_snapshot)
                logger.info(f"[Callback] MIXED pairs collected: {[p.get('original_query') for p in pairs]}")

                # Store pairs and clear direct state fields
                callback_context.state["multi_query_pairs"] = pairs
                callback_context.state["query_results"] = None
                callback_context.state["answer_plan"] = None
                callback_context.state["data_crawled"] = None

    except Exception as e:
        logger.error(f"Error in execute_query_tool_callback: {e}")
        traceback.print_exc()

    return llm_response


def after_query_agent_callback(callback_context: CallbackContext):
    """Post-agent cleanup: reset all query state when is_query is False."""
    is_query = callback_context.state.get("is_query", False)
    logger.info(f"after_query_agent_callback checked is_query: {is_query}")

    if not is_query:
        # Reset all query-related state so stale data doesn't bleed through
        keys_to_reset = [
            "query_results",
            "confidence_score",
            "has_media",
            "confidence_score_action_results",
            "confidence_score_answer_guidance",
            "confidence_score_action_answer_guidance",
            "data_lv2_data_enriched_nodes",
            "data_lv2_siblings_relative",
            "data_lv2_siblings_similar",
            "data_lv2_data_enriched_relation",
            "data_lv2_related_nodes",
            "multi_query_pairs",
            "is_mixed_query",
        ]
        for key in keys_to_reset:
            callback_context.state[key] = None

        current_extra_data = callback_context.state.get("extra_data")
        if isinstance(current_extra_data, dict):
            current_extra_data["attachments"] = None
            callback_context.state["extra_data"] = current_extra_data
    return None
