import ast
import json
from typing import Any

import httpx
import requests
from google.adk.agents.callback_context import CallbackContext
from google.adk.tools import ToolContext

from configs.config_service import get_settings
from dbs.milvus_helper import insert_crawled_data, search_common_qa, search_crawled_data
from schemas.query_agents import QueryAgentOutputSchemaV2
from tools.confident_query_hint_processing import ConfidentQueryHintProcessor
from tools.why_query_plan_dmn_processing import QueryPlanDMNProcessor
from utils.calculate_confidence_score_query_results import calculate_confidence_score
from utils.logging_config import get_logger
from utils.parallel_web_search import run_parallel_web_search_sync

logger = get_logger(__name__)

settings = get_settings()
COMMON_DATA_COLLECTION = "knowledge_common_qa"


def save_search_results(
    tool_context,
    answer: str,
    question: str,
    # references: str = "",
    media_data: dict[str, Any] | None = None,
    type_hint: str = "",
) -> dict[str, Any]:
    """
    Save search results to session state with references and media.

    Args:
            tool_context: ADK ToolContext with state access
            answer: Vietnamese answer from knowledge base
            question: Original user question
            references: JSON string of references with page_ref and doc_id
                               Format: [{"page_ref": "...", "doc_id": "...", "source": "..."}]
            media_data: Optional media attachments (NEW)
                               Format: {"attachments": [{"url": "...", "file_type": "...", ...}]}
            type_hint

    Returns:
            Confirmation dict with counts
    """
    logger.info(" Saving search results to state...")
    #
    # # Parse references if provided
    # parsed_references = []
    # if references:
    # 	try:
    # 		parsed_references = json.loads(references) if isinstance(references, str) else references
    # 		logger.info(f"   Saved {len(parsed_references)} references")
    # 	except Exception as e:
    # 		parsed_references = []

    # # Save query results (text)
    # tool_context.state["query_results"] = str({
    # 	"answer": answer,
    # 	"question": question,
    # 	"status": "success",
    # 	"is_new_question": True  # Mark as new question for response_polishing_agent
    # })
    # --- SAVE MEDIA TO extra_data ---
    if media_data and media_data.get("attachments"):
        # Read existing extra_data (may contain buttons, etc.)
        existing_extra_data = {}
        if "extra_data" in tool_context.state:
            try:
                existing_extra_data = tool_context.state["extra_data"]
            except Exception as e:
                logger.error(f" Failed to parse existing extra_data: {e}")
                existing_extra_data = {}

        # Merge media attachments into extra_data
        existing_extra_data["attachments"] = media_data["attachments"]

        # Save back to state
        tool_context.state["extra_data"] = existing_extra_data
        tool_context.state["has_media"] = bool(media_data and media_data.get("attachments"))

        logger.info(f" Saved {len(media_data['attachments'])} media attachments to extra_data")
    else:
        logger.info(" No media attachments to save")
        tool_context.state["has_media"] = False
        current_extra_data = tool_context.state["extra_data"]
        current_extra_data["attachments"] = None
        tool_context.state["extra_data"] = current_extra_data

    logger.info(" Results saved to state")
    # type_hint = tool_context.state.get("type_hint", "")
    if type_hint == "confidence_score":
        logger.info("Saving output to confidence_score_action_results")
        tool_context.state["confidence_score_action_results"] = str(
            {
                "answer": answer,
                "question": question,
                "status": "success",
            }
        )
    else:
        tool_context.state["query_results"] = str(
            {
                "answer": answer,
                "question": question,
                "status": "success",
                "is_new_question": True,
            }
        )

    # tool_context.actions.skip_summarization = True

    return {"status": "saved", "answer": answer}


http_client = httpx.AsyncClient(base_url=f"http://{settings.mcp_host}:{settings.mcp_port}", timeout=30.0)


def _is_answer_empty(answer) -> bool:
    """Check if answer is effectively empty.

    Returns True if:
    - answer is falsy (None, empty string, empty dict, empty list)
    - answer is a dict where ALL values that are dicts are also empty
    """
    if not answer:
        return True
    if isinstance(answer, dict):
        # Check if all values in the dict are empty
        for value in answer.values():
            if isinstance(value, dict):
                if value:  # non-empty dict means there's data
                    return False
            elif value:  # any non-falsy, non-dict value means there's data
                return False
        return True
    return False


def _search_common_data_query_results(original_query: str) -> str | None:
    """Search common_data before invoking DMN and normalize hits into query_results."""
    if not original_query:
        return None

    try:
        common_data_results = search_common_qa(
            question=original_query,
            top_k=2,
            threshold=0.6,
            min_final_score=0.7,
            collection_name=COMMON_DATA_COLLECTION,
        )
    except Exception as e:
        logger.error(f"Failed to search common_data before DMN: {e}")
        return None

    if not common_data_results:
        return None

    return json.dumps(
        {
            "question": original_query,
            "answer": common_data_results,
            "status": "success",
            "source": COMMON_DATA_COLLECTION,
            "is_new_question": True,
        },
        ensure_ascii=False,
    )


def _get_common_data_query_candidates(
    tool_context: ToolContext | CallbackContext,
    fallback_original_query: str | None,
) -> list[tuple[str, str]]:
    """Build ordered common-QA lookup candidates with de-duplication."""
    raw_candidates = [
        ("original_user_message", tool_context.state.get("original_user_message")),
        ("final_rewritten_user_message", tool_context.state.get("final_rewritten_user_message")),
        ("original_query", fallback_original_query),
    ]

    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()

    for source_name, candidate in raw_candidates:
        if not isinstance(candidate, str):
            continue

        stripped = candidate.strip()
        if not stripped:
            continue

        dedupe_key = stripped.casefold()
        if dedupe_key in seen:
            continue

        seen.add(dedupe_key)
        candidates.append((source_name, stripped))

    return candidates


def _search_common_data_with_fallback(
    tool_context: ToolContext | CallbackContext,
    fallback_original_query: str | None,
) -> str | None:
    """Search common QA using original user text first, then rewritten/query variants."""
    candidates = _get_common_data_query_candidates(tool_context, fallback_original_query)
    tool_context.state["matched_query_variant"] = None

    for source_name, candidate in candidates:
        common_data_results = _search_common_data_query_results(candidate)
        if common_data_results:
            tool_context.state["matched_query_variant"] = source_name
            logger.info(
                "Common QA matched using candidate '%s': %s",
                source_name,
                candidate,
            )
            return common_data_results

    return None


def query_internal_data(
    tool_context: ToolContext | CallbackContext,
    input_tool: QueryAgentOutputSchemaV2,
    type_hint: str = "",
    skip_state_reset: bool = False,
    return_result: bool = False,
):
    """
    Query knowledge base with optional media fetching.

    Args:
            tool_context: ADK ToolContext with state access
            input_tool: QueryAgentOutputSchema with query parameters
            type_hint: Hint for response type
            skip_state_reset: When True, skip STEP 0 state reset (used in MIXED mode where
                              reset is done once in the callback before parallel execution)
            return_result: When True, return collected result dict instead of writing to state
                          (used in MIXED mode where each sub-query runs in its own thread)
    """
    # =================================================================
    # STEP 0: RESET QUERY STATE
    # =================================================================
    if not skip_state_reset:
        logger.info("[QueryAgent] Resetting all query-related state keys before processing new query")

        # Core query results and flags
        tool_context.state["query_results"] = None
        tool_context.state["confidence_score"] = None
        tool_context.state["has_media"] = None
        tool_context.state["is_required_media"] = None
        tool_context.state["answer_plan"] = None

        # Query planning and execution state
        tool_context.state["primary_entities"] = None
        tool_context.state["context_entities"] = None
        tool_context.state["compare_targets"] = None
        tool_context.state["potential_entities"] = None
        tool_context.state["subtopics"] = None
        tool_context.state["time_filter"] = None
        tool_context.state["time_compare"] = None
        tool_context.state["original_query"] = None

        # Data sources
        tool_context.state["data_crawled"] = None

        # Confidence score action keys
        tool_context.state["confidence_score_action_results"] = None
        tool_context.state["confidence_score_answer_guidance"] = None
        tool_context.state["confidence_score_action_answer_guidance"] = None

        # Data lv2 reasoning keys
        tool_context.state["data_lv2_data_enriched_nodes"] = None
        tool_context.state["data_lv2_siblings_relative"] = None
        tool_context.state["data_lv2_siblings_similar"] = None
        tool_context.state["data_lv2_data_enriched_relation"] = None
        tool_context.state["data_lv2_related_nodes"] = None

        # Query type and intent
        tool_context.state["qtype"] = None
        tool_context.state["intent"] = None
        tool_context.state["count_enumerate_targets"] = None
        tool_context.state["topic"] = None
        tool_context.state["compare_mode"] = None
        tool_context.state["need_disambig"] = None
        tool_context.state["question_type"] = None
        tool_context.state["entity"] = None
        tool_context.state["matched_query_variant"] = None

        # Playbook action results
        # tool_context.state["playbook_action_results"] = None

        # Reset attachments in extra_data without clearing the entire extra_data
        if "extra_data" in tool_context.state:
            try:
                current_extra_data = tool_context.state["extra_data"]
                if isinstance(current_extra_data, dict):
                    current_extra_data["attachments"] = None
                    tool_context.state["extra_data"] = current_extra_data
            except Exception as e:
                logger.error(f" Failed to reset attachments in extra_data: {e}")

        logger.info("[QueryAgent] All query-related state keys have been reset")
    else:
        logger.debug("[QueryAgent] Skipping state reset (skip_state_reset=True, reset handled by caller)")

    # =================================================================
    # STEP 1: EXTRACT VALUES FROM AGENT OUTPUT
    # =================================================================
    target_entities = "input_tool.target_entities"
    related_entities_type = "input_tool.related_entities_type"
    related_entities = "input_tool.related_entities"
    target_attribute = "input_tool.target_attribute"
    related_entities_attribute = "input_tool.related_entities_attribute"
    query_type = "input_tool.query_type"
    original_query = input_tool.original_query
    is_query = input_tool.is_query
    is_required_media = "input_tool.is_required_media"
    effective_from = "input_tool.effective_from"
    effective_to = "input_tool.effective_to"

    # NEW SCHEMA FIELDS
    question_type = input_tool.question_type
    intent = input_tool.intent
    primary_topic = input_tool.primary_topic
    primary_entities = [
        e.model_dump() if hasattr(e, "model_dump") else dict(e) for e in (input_tool.primary_entities or [])
    ]
    context_entities = [
        e.model_dump() if hasattr(e, "model_dump") else dict(e) for e in (input_tool.context_entities or [])
    ]
    compare_mode = input_tool.compare_mode
    compare_targets = [
        e.model_dump() if hasattr(e, "model_dump") else dict(e) for e in (input_tool.compare_targets or [])
    ]
    subtopics = input_tool.subtopics
    needs_disambiguation = input_tool.needs_disambiguation
    time = input_tool.time
    time_compare = input_tool.time_compare
    potential_entities = [
        e.model_dump() if hasattr(e, "model_dump") else dict(e) for e in (input_tool.potential_entities or [])
    ]
    count_enumerate_targets = input_tool.count_enumerate_targets

    logger.debug(
        "run query_internal_data with question_type=%s, target_entities=%s, query_type=%s",
        question_type,
        target_entities,
        query_type,
    )

    api_url = f"http://{settings.mcp_host}:{settings.mcp_port}"
    if isinstance(tool_context, ToolContext):
        tool_context.actions.skip_summarization = True

    # Store flags
    tool_context.state["is_query"] = is_query
    tool_context.state["is_required_media"] = is_required_media

    # --- BRANCHING LOGIC ---
    # IF question_type is NEW TYPE (WHY, HOW, WHICH, MIXED) -> Handle separately
    if question_type in ["WHY", "HOW", "WHAT"]:
        logger.info("Handling NEW QUESTION TYPE: %s", question_type)
        tool_context.state["input_data"] = input_tool.model_dump()
        # For now, we return the extracted data to confirm extraction works.
        tool_context.state["qtype"] = question_type
        tool_context.state["intent"] = intent
        tool_context.state["topic"] = primary_topic
        tool_context.state["compare_mode"] = compare_mode
        tool_context.state["need_disambig"] = needs_disambiguation
        tool_context.state["time_filter"] = time
        tool_context.state["time_compare"] = (
            time_compare.model_dump() if hasattr(time_compare, "model_dump") else time_compare
        )
        tool_context.state["primary_entities"] = primary_entities
        tool_context.state["context_entities"] = context_entities
        tool_context.state["compare_targets"] = compare_targets
        tool_context.state["subtopics"] = subtopics
        tool_context.state["potential_entities"] = potential_entities
        tool_context.state["count_enumerate_targets"] = count_enumerate_targets
        tool_context.state["original_query"] = original_query

        common_data_query_results = _search_common_data_with_fallback(tool_context, original_query)
        if common_data_query_results:
            logger.info("Skipping QueryPlanDMNProcessor because common_data already returned results.")
            tool_context.state["query_results"] = common_data_query_results
        else:
            query_orchestrator = QueryPlanDMNProcessor()
            query_orchestrator.process_query_plan(context=tool_context)

        # Save basic info to state
        tool_context.state["question_type"] = question_type
        tool_context.state["intent"] = intent

        if return_result:
            # Collect results from state for MIXED mode caller
            return {
                "original_query": original_query,
                "question_type": question_type,
                "query_results": tool_context.state.get("query_results"),
                "answer_plan": tool_context.state.get("answer_plan"),
                "data_crawled": tool_context.state.get("data_crawled"),
                "data_lv2_data_enriched_nodes": tool_context.state.get("data_lv2_data_enriched_nodes"),
                "data_lv2_siblings_relative": tool_context.state.get("data_lv2_siblings_relative"),
                "data_lv2_siblings_similar": tool_context.state.get("data_lv2_siblings_similar"),
                "data_lv2_data_enriched_relation": tool_context.state.get("data_lv2_data_enriched_relation"),
                "data_lv2_related_nodes": tool_context.state.get("data_lv2_related_nodes"),
                "confidence_score": tool_context.state.get("confidence_score"),
                "confidence_score_action_results": tool_context.state.get("confidence_score_action_results"),
                "confidence_score_answer_guidance": tool_context.state.get("confidence_score_answer_guidance"),
            }

        return None

    # ELIF Normal WHAT/Legacy flow -> Continue with existing logic
    target_entities = target_entities if target_entities is not None else []
    related_entities_type = related_entities_type if related_entities_type is not None else []
    related_entities_attribute = related_entities_attribute if related_entities_attribute is not None else []
    related_entities = related_entities if related_entities is not None else []
    target_attribute = target_attribute if target_attribute is not None else []

    result = ""

    # --- DEFINITION QUERY ---
    if query_type == "definition":
        response = requests.post(
            url=f"{api_url}/search_vector_db", json={"target_entities": target_entities, "top_k": 1, "threshold": 0.7}
        )
        response.raise_for_status()
        result = response.json()

    # --- ATTRIBUTE QUERY ---
    elif query_type == "attribute":
        response = requests.post(
            url=f"{api_url}/attribute_search",
            json={
                "target_entities": target_entities,
                "related_entities_type": related_entities_type,
                "related_entities": related_entities,
                "original_query": original_query,
                "target_attribute": target_attribute,
                "related_entities_attribute": related_entities_attribute,
                "is_required_media": is_required_media,
                "effective_from": effective_from,
                "effective_to": effective_to,
            },
        )
        response.raise_for_status()
        result = response.json()

    # --- ENUMERATION QUERY ---
    elif query_type == "enumeration":
        response = requests.post(
            url=f"{api_url}/enumeration_search",
            json={
                "target_entities": target_entities,
                "related_entities_type": related_entities_type,
                "related_entities": related_entities,
                "original_query": original_query,
                "target_attribute": target_attribute,
                "related_entities_attribute": related_entities_attribute,
                "is_required_media": is_required_media,
                "effective_from": effective_from,
                "effective_to": effective_to,
            },
        )
        response.raise_for_status()
        result = response.json()

    # --- COMPARISON QUERY ---
    elif query_type == "comparison":
        response = requests.post(
            url=f"{api_url}/comparison_search",
            json={
                "target_entities": target_entities,
                "related_entities_type": related_entities_type,
                "original_query": original_query,
                # "requested_attributes": requested_attribute
            },
        )
        response.raise_for_status()
        result = response.json()

    else:
        # --- RESET ALL QUERY-RELATED STATE KEYS WHEN is_query=False ---
        logger.info(" Resetting query-related state keys (is_query=False)")

        # Core query flags and results
        tool_context.state["query_results"] = None
        tool_context.state["confidence_score"] = None
        tool_context.state["has_media"] = None

        # Confidence score action keys
        tool_context.state["confidence_score_action_results"] = None
        tool_context.state["confidence_score_answer_guidance"] = None
        tool_context.state["confidence_score_action_answer_guidance"] = None

        # Data lv2 reasoning keys
        tool_context.state["data_lv2_data_enriched_nodes"] = None
        tool_context.state["data_lv2_siblings_relative"] = None
        tool_context.state["data_lv2_siblings_similar"] = None

        # Reset attachments in extra_data
        if "extra_data" in tool_context.state:
            try:
                current_extra_data = tool_context.state["extra_data"]
                if isinstance(current_extra_data, dict):
                    current_extra_data["attachments"] = None
                    tool_context.state["extra_data"] = current_extra_data
            except Exception as e:
                logger.error(f" Failed to reset attachments in extra_data: {e}")

        logger.info(" All query-related state keys reset to None")

    # --- SAVE RESULTS IF QUERY ---
    if is_query:
        # tool_context.state["target_entities"] = target_entities
        # tool_context.state["related_entities_type"] = related_entities_type
        # tool_context.state["related_entities"] = related_entities
        # tool_context.state["requested_attribute"] = requested_attribute
        # Extract text answer and media from result
        if isinstance(result, dict):
            answer = result.get("results", result)  # Text answer
            media_data = result.get("media", None)  # Media attachments (optional)
        else:
            answer = result
            media_data = None

        # Log media extraction
        if not answer or _is_answer_empty(answer):
            # Try searching in crawled data collection first
            query_web_search = original_query
            crawled_results = search_crawled_data(question=query_web_search, top_k=1, threshold=0.71)

            if crawled_results:
                # Found in collection - use cached result
                logger.info("[QueryOrchestrator] Found in crawled data collection: %s results", len(crawled_results))
                data_crawled = crawled_results[0]["content"]
                tool_context.state["data_crawled"] = data_crawled
            else:
                # Not found in collection - perform web search and cache result
                logger.info("[QueryOrchestrator] Not found in collection, performing web search...")
                web_search_result = run_parallel_web_search_sync(query=query_web_search, is_school=True)

                # Extract data from the result dictionary
                data_crawled = web_search_result.get("summary", "")
                source_types = web_search_result.get("source_types", [])
                source_urls = web_search_result.get("source_urls", [])

                # Insert the result into collection for future use
                insert_success = insert_crawled_data(
                    question=query_web_search,
                    content=data_crawled,
                    source_type=source_types,
                    source_url=source_urls,
                    supporting_node_type=[],
                )

                if insert_success:
                    logger.info("[QueryOrchestrator] Successfully cached web search result in collection")
                else:
                    logger.error("[QueryOrchestrator] Failed to cache web search result")

            # tool_context.state["data_crawled"] = data_crawled
            tool_context.state["data_crawled"] = data_crawled

        if media_data and media_data.get("attachments"):
            logger.info(f" Extracted {len(media_data['attachments'])} media attachments from API")

        tool_context.state["original_query"] = original_query
        meta_data_query = result["meta_data_query"] if "meta_data_query" in result else {}
        meta_input_query = result["meta_input_query"] if "meta_input_query" in result else {}
        if meta_data_query is None and meta_input_query is None:
            confidence_score = 0.0
        else:
            confidence_score = calculate_confidence_score(
                meta_data_query=meta_data_query, meta_input_query=meta_input_query
            )
            logger.debug("confidence_score: %s", confidence_score)

        tool_context.state["confidence_score"] = confidence_score
        logger.info(f"Calculated confidence score: {confidence_score}")
        for target_entity in meta_data_query.get("target_entities", []):
            if target_entity["node_type"] == "AcademicProgram":
                tool_context.state["entity"] = "AcademicProgram"
            else:
                tool_context.state["entity"] = target_entity["node_type"]
        confident_query_hint_processor = ConfidentQueryHintProcessor()
        hint_response = confident_query_hint_processor.process_hints(context=tool_context)
        logger.debug("hint_response: %s", hint_response)
        if isinstance(hint_response, dict):
            if hint_response.get("confidence_score_action_results", None) is not None:
                answer = ast.literal_eval(tool_context.state["confidence_score_action_results"])
        # Save to state
        save_search_results(
            tool_context=tool_context,
            question=original_query,
            answer=answer,
            media_data=media_data,
            type_hint=type_hint,
        )
        # Only process hints when is_query=True

        #
        # target_node_labels = extract_unique_node_labels(meta_data_query)
        # logger.error(f"target_node_labels: {target_node_labels}")
        # node_ids = extract_unique_node_ids(meta_data_query).get("node_ids")

        # Prepare input for reasoning from the answer (results)
        # reasoning_input = prepare_reasoning_input(answer=answer)
        # logger.error(f"reasoning_input: {reasoning_input}")
        #
        # # Call process_reasoning if we have valid input
        # if reasoning_input:
        # 	try:
        # 		reasoning_result = process_reasoning(
        # 			tool_context=tool_context,
        # 			input_data=reasoning_input,
        # 			enrich_top_k=5,
        # 			similarity_threshold=0.7
        # 		)
        # 		logger.error(f"reasoning_result: {reasoning_result}")
        # 	except Exception as e:
        # 		logger.error(f"Error in process_reasoning: {e}")

        # logger.error(f"meta_data_query: {meta_data_query}")
        # logger.error(f"meta_input_query: {meta_input_query}")
        return {"answer": answer, "meta_data_query": meta_data_query}

    return None
