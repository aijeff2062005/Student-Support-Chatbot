from google.adk.agents.callback_context import CallbackContext
from google.adk.models import LlmRequest
from google.genai import types

from configs.config_service import get_settings
from utils.logging_config import get_logger

logger = get_logger(__name__)
settings = get_settings()


def query_response_filter_callback(callback_context: CallbackContext) -> types.Content | None:
    """
    After-agent callback to filter out responses for non-query messages.

    This callback checks state.is_query:
    - If is_query = False (data collection) → Return empty Content (suppress response)
    - If is_query = True (actual query) → Return None (keep agent's response)

    This ensures query_response_agent ONLY responds to factual questions,
    and stays completely silent for data collection inputs such as
    "I am a student."

    Args:
        callback_context: CallbackContext containing state

    Returns:
        None to keep agent's response, or empty Content to suppress
    """
    # Get is_query from state (set by query_analyzer_agent)
    # logger.error(f"Full state: {callback_context.state.to_dict()}")
    # logger.error(f"State items:")
    # for key, value in callback_context.state.to_dict().items():
    # 	logger.error(f"  {key} = {value} (type: {type(value)})")

    # logger.error("=" * 80)
    # logger.error(" CALLBACK TRIGGERED!")
    # logger.error(f"Agent: {callback_context.agent_name}")
    # logger.error(f"Invocation ID: {callback_context.invocation_id}")
    # logger.error(f"State keys: {list(callback_context.state.to_dict().keys())}")
    # logger.error(f"State dict: {callback_context.state.to_dict()}")
    # logger.error("=" * 80)
    is_query = callback_context.state.get("is_query", True)  # Default True for safety

    logger.debug("Query filter callback: is_query=%s", is_query)

    # If not a query (data collection), suppress response completely
    if not is_query:
        logger.info("Not a query - suppressing query_response_agent response")
        # Return empty Content to override and suppress agent's output
        return types.Content(parts=[types.Part(text=None)], role="model")

    # If it's a query, keep agent's original response
    logger.debug("Is a query - keeping query_response_agent response")
    return types.Content(parts=[], role="model")


def get_raw_user_input(callback_context: CallbackContext) -> types.Content | None:
    """
    After-agent callback to filter out responses for non-query messages.

    This callback checks state.is_query:
    - If is_query = False (data collection) → Return empty Content (suppress response)
    - If is_query = True (actual query) → Return None (keep agent's response)

    This ensures query_response_agent ONLY responds to factual questions,
    and stays completely silent for data collection inputs such as
    "I am a student."

    Args:
        callback_context: CallbackContext containing state

    Returns:
        None to keep agent's response, or empty Content to suppress
    """
    # Get is_query from state (set by query_analyzer_agent)
    # logger.error(f"Full state: {callback_context.state.to_dict()}")
    # logger.error(f"State items:")
    # for key, value in callback_context.state.to_dict().items():
    # 	logger.error(f"  {key} = {value} (type: {type(value)})")

    # logger.error("=" * 80)
    # logger.error(" CALLBACK TRIGGERED!")
    # logger.error(f"Agent: {callback_context.agent_name}")
    # logger.error(f"Invocation ID: {callback_context.invocation_id}")
    # logger.error(f"State keys: {list(callback_context.state.to_dict().keys())}")
    # logger.error(f"State dict: {callback_context.state.to_dict()}")
    # logger.error("=" * 80)
    raw_user_input = callback_context._event_actions
    logger.debug("Raw user input received: %s", raw_user_input)
    return None


def log_llm_request(callback_context: CallbackContext, llm_request: LlmRequest) -> types.Content | None:
    """A callback to print the LLM request."""
    logger.debug("LLM Request: %s", llm_request)
    return None
