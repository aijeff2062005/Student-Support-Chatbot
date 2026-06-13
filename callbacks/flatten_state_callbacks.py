from copy import deepcopy
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.tools import BaseTool, ToolContext
from google.genai import types

from utils.logging_config import get_logger

logger = get_logger(__name__)


def flatten_state_callback(callback_context: CallbackContext):
    """Flattens nested user info from the state into top-level keys."""
    current_state = callback_context.state.to_dict()
    # print("Current nested state:", current_state)

    if "user_state" in current_state and isinstance(current_state["user_state"], dict):
        customer_info = current_state["customer_info"] if "customer_info" in current_state else None
        user_state = current_state["user_state"]
        student_profile = user_state.get("student_profile")
        user_major = user_state.get("major")
        # if user_major is None:
        #     user_major = user_state.get("major", "")
        # Flatten the nested data
        callback_context.state.update(
            {
                "role": user_state.get("role", None),
                "customer_gender": customer_info.get("gender", None) if customer_info else None,
                "student_gender": student_profile.get("gender", None) or current_state.get("student_gender", None),
                "student_province_location": student_profile.get("province_location", None)
                or current_state.get("student_province_location", None),
                "old_province_location": student_profile.get("old_province_location", None)
                or current_state.get("old_province_location", None),
                "student_name": student_profile.get("full_name", ""),
                "student_phone": student_profile.get("phone", ""),
                "student_email": student_profile.get("email", ""),
                "student_high_school": student_profile.get("high_school", ""),
                "student_high_school_province": student_profile.get("high_school_province", ""),
                "old_high_school_province": student_profile.get("old_high_school_province", None)
                or current_state.get("old_high_school_province", None),
                "student_high_school_address": student_profile.get("high_school_address", ""),
                "student_admission_methods": student_profile.get("admission_methods", []),
                "user_topic": str(user_state.get("topic", "")),
                "user_major": user_major,
                "is_requested_submit_application_now": user_state.get("is_requested_submit_application_now", False),
                "potential_majors": user_state.get("potential_majors", []),
            }
        )
        # print("State after flattening:", callback_context.state.to_dict())

    # Return None to proceed with the agent's normal execution
    return None


def log_output_for_tool(
    tool: BaseTool, args: dict[str, Any], tool_context: ToolContext, tool_response: dict
) -> dict | None:
    """Inspects/modifies the tool result after execution."""
    agent_name = tool_context.agent_name
    tool_name = tool.name
    logger.debug("[Callback] After tool call for tool '%s' in agent '%s'", tool_name, agent_name)
    logger.debug("[Callback] Args used: %s", args)
    logger.debug("[Callback] Original tool_response: %s", tool_response)

    # Default structure for function tool results is {"result": <return_value>}
    original_result_value = tool_response.get("result", "")
    # original_result_value = tool_response

    # --- Modification Example ---
    # If the tool was 'get_capital_city' and result is 'Washington, D.C.'
    if tool_name == "query_internal_data":
        logger.debug("[Callback] Tool response: %s", original_result_value)

        # IMPORTANT: Create a new dictionary or modify a copy
        modified_response = deepcopy(tool_response)
        # modified_response["result"] = f"{original_result_value} (Note: This is the capital of the USA)."
        # modified_response["note_added_by_callback"] = True # Add extra info if needed
        #
        # print(f"[Callback] Modified tool_response: {modified_response}")
        return modified_response  # Return the modified dictionary

    # print("[Callback] Passing original tool response through.")
    # Return None to use the original tool_response
    return None


def log_output_for_agent(callback_context: CallbackContext) -> types.Content | None:
    """
    Logs exit from an agent and checks 'add_concluding_note' in session state.
    If True, returns new Content to *replace* the agent's original output.
    If False or not present, returns None, allowing the agent's original output to be used.
    """
    current_state = callback_context.state.to_dict()

    logger.debug("[Callback] Current Question Result: %s", current_state.get("question_analysis_result"))

    # Example: Check state to decide whether to modify the final output
    if current_state.get("add_concluding_note", False):
        # print(f"[Callback] State condition 'add_concluding_note=True' met: Replacing agent {agent_name}'s output.")
        # Return Content to *replace* the agent's own output
        return types.Content(
            parts=[types.Part(text="Concluding note added by after_agent_callback, replacing original output.")],
            role="model",  # Assign model role to the overriding response
        )
    else:
        # print(f"[Callback] State condition not met: Using agent {agent_name}'s original output.")
        # Return None - the agent's output produced just before this callback will be used.
        return None
