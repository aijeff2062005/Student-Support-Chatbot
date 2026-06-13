"""Combined Orchestrator - Stage Management + Data Collection.

Single orchestrator that runs both collector_intent and segment_collection in sequence.
"""

import logging
from importlib import import_module
from typing import Any

from google.adk.tools import FunctionTool
from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)


async def combined_orchestrator(tool_context: ToolContext, user_message: str) -> dict[str, Any]:
    """Combined orchestrator that runs stage management then data collection.

    Hard-coded sequential execution:
    1. Run collector_intent_orchestrator (stage management)
    2. Run segment_collection_orchestrator (data collection)

    Args:
        tool_context: ADK ToolContext with state
        user_message: User's input message

    Returns:
        Dict with combined results from both orchestrators
    """
    logger.info("=" * 80)
    logger.info(" COMBINED ORCHESTRATOR START")
    logger.info(f"Message: {user_message[:60]}...")
    logger.info("=" * 80)

    # Resolve optional orchestrators lazily to avoid import-time hard failures.
    collector_intent_orchestrator = None
    segment_collection_orchestrator = None
    try:
        collector_module = import_module("agents.collector_intent")
        collector_intent_orchestrator = getattr(collector_module, "collector_intent_orchestrator", None)
    except Exception:
        logger.exception("Failed to import collector_intent_orchestrator")

    try:
        segment_module = import_module("tools.data_collection_tools")
        segment_collection_orchestrator = getattr(segment_module, "segment_collection_orchestrator", None)
    except Exception:
        logger.exception("Failed to import segment_collection_orchestrator")

    # STEP 1: Stage Management (always run first)
    logger.info(" Step 1/2: Running collector_intent_orchestrator...")
    try:
        if callable(collector_intent_orchestrator):
            intent_result = await collector_intent_orchestrator(tool_context, user_message)
            logger.info(f"Stage: {intent_result.get('stage')}")
        else:
            logger.warning("collector_intent_orchestrator not available")
            intent_result = {}
    except Exception as e:
        logger.error(f"collector_intent_orchestrator failed: {e}")
        intent_result = {}

    # STEP 2: Data Collection (always run second)
    logger.info(" Step 2/2: Running segment_collection_orchestrator...")
    try:
        if callable(segment_collection_orchestrator):
            collection_result = await segment_collection_orchestrator(tool_context, user_message)
            logger.info(f"Completeness: {collection_result.get('completeness_percentage', 0)}%")
            logger.info(f"Next question: {collection_result.get('next_question', 'N/A')[:50]}...")
        else:
            logger.warning("segment_collection_orchestrator not available")
            collection_result = {}
    except Exception as e:
        logger.error(f"segment_collection_orchestrator failed: {e}")
        collection_result = {}

    # Combine results
    combined_result = {
        "intent": intent_result,
        "collection": collection_result,
        "stage": intent_result.get("stage"),
        "ctas": intent_result.get("ctas", []),
        "completeness_percentage": collection_result.get("completeness_percentage", 0),
        "next_question": collection_result.get("next_question", ""),
        "segmentation_triggered": collection_result.get("segmentation_triggered", False),
    }

    logger.info("=" * 80)
    logger.info(" COMBINED ORCHESTRATOR COMPLETE")
    logger.info(f"Stage: {combined_result['stage']}")
    logger.info(f"Completeness: {combined_result['completeness_percentage']}%")
    logger.info("=" * 80)

    return combined_result


def get_combined_orchestrator_tool() -> list[Any]:
    """Get combined orchestrator tool.

    Returns:
        List with single combined orchestrator tool
    """
    return [FunctionTool(combined_orchestrator)]
