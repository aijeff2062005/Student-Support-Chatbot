from google.adk.tools.tool_context import ToolContext

from callbacks.confidence_score_dmn_callback import transfer_to_qna_agent


def handle_action_type_query(hint_data: dict, tool_context: ToolContext, type_hint: str) -> None:
    """
    Handle action hints of type "query" by extracting query parameters
    and storing them in the context state.

    Args:
            hint_data: The action hint data dictionary.
            tool_context: The tool context state dictionary to store results.
            type_hint
    """
    if hint_data.get("question"):
        # tool_context.state["agent_qna_mode"] = "lite"
        transfer_to_qna_agent(tool_context, type_hint=type_hint)

    elif params := hint_data.get("params"):
        params.get("is_query")
        pass
        # context_state["confidence_score_action_is_query"] = is_query
