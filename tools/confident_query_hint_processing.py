import logging
import traceback
from enum import StrEnum
from typing import Any

import requests
from google.adk.tools.tool_context import ToolContext

from configs.config_service import get_settings
from dbs.graph_search_helpers import sanitize_neo4j_types
from dbs.milvus_helper import search_entity_by_name
from tools.QA.services.neo4j_service import Neo4jService

neo4j_service = Neo4jService()

settings = get_settings()

logger = logging.getLogger("confident_query_hint_processor")


class AnswerHintTemplateParam(StrEnum):
    """Enumeration for answer hint template parameters."""

    USER_PRONOUN = "user_pronoun"
    SELF_PRONOUN = "self_pronoun"


class ActionHintTemplateParam(StrEnum):
    """Enumeration for action hint template parameters."""

    PERSONALITY_PROFILE = "personality_profile"
    MAJOR = "major"


def fallback_major_not_have_acapro(context: ToolContext):
    state = context.state.to_dict()
    original_query = state.get("original_query")
    if not isinstance(original_query, str) or not original_query.strip():
        return {}

    entity_type = ["Major", "Specialization"]
    new_target_entities = search_entity_by_name(
        entity_name=[original_query], entity_type=entity_type, top_k=1, threshold=0.3
    )
    if not new_target_entities:
        return {}

    logger.info(f"new_target_entities: {new_target_entities}")
    new_target_entities_node_id = new_target_entities[0].get("node_id")
    if not isinstance(new_target_entities_node_id, str) or not new_target_entities_node_id:
        return {}

    attribute_keys = new_target_entities[0].get("attribute_keys")
    if not isinstance(attribute_keys, list):
        attribute_keys = []

    new_target_entities_attributes = neo4j_service.get_node_attribute(
        node_ids=[new_target_entities_node_id], attributes_keys=attribute_keys
    )
    results = sanitize_neo4j_types(new_target_entities_attributes)
    logger.info(f"fallback_major_not_have_acapro: {results}")
    return results


class ConfidentQueryHintProcessor:
    """Processor for fetching and handling confident query hints."""

    def __init__(self):
        """
        Initialize the ConfidentQueryHintProcessor.

        Args:
            api_endpoint: The URL of the confident query hint service.
        """
        self.kogito_url = settings.kogito_endpoint_addressing_rule
        self.api_endpoint = f"{self.kogito_url}/confident_query_result"

    def process_hints(self, context: ToolContext) -> dict[str, Any]:
        """
        Main entry point to process hints based on tool context for Confident Query.

        Args:
            context: The tool context containing state.

        Returns:
            The raw response from the DMN API (or empty dict on failure),
            augmented with processed hint actions.
        """
        # 1. Extract context data
        payload = self._extract_confidence_score_context_data(context)

        # 2. Call DMN API
        response_data = self._call_dmn_confidence_score_api(payload)
        if not response_data:
            return {}

        # 3. Process outputs
        output = response_data.get("ConfidentQueryResult", {})
        processed_results = {}
        if output:
            answer_hint = output.get("answer_hint")
            if answer_hint:
                self._handle_confidence_score_answer_hint(answer_hint, context)
                processed_results["confidence_score_answer_guidance"] = context.state.get(
                    "confidence_score_answer_guidance"
                )

            cta_hints = output.get("cta_hint", [])
            if cta_hints:
                self._handle_confidence_score_cta_hint(cta_hints, context)
                processed_results["cta_hint"] = context.state.get("cta_hint")

            action_hints = output.get("action_hint", [])
            # list_action_hints = ast.literal_eval(action_hints)
            if action_hints:
                self._handle_confidence_score_action_hint(action_hints, context)
                logger.info("Confident Query Hint Action Hints: %s", processed_results)
                processed_results["confidence_score_action_results"] = context.state.get(
                    "confidence_score_action_results"
                )

            return processed_results
        return {}

    def _extract_confidence_score_context_data(self, context: ToolContext) -> dict[str, Any]:
        """
        Extracts relevant fields from ToolContext state for Confident Query.

        Args:
            context: The tool context.

        Returns:
            Dictionary matching the Confident Query DMN API input schema.
        """
        state = context.state
        logger.info(f"Extracted Confident Query DMN API input schema: {state.get('confidence_score')}")
        confidence_score = state.get("confidence_score", {}).get("total_confidence", 0.0)
        score = int(confidence_score * 100)
        logger.info(f"Confident query hint score: {score}")
        data = {
            "confident_score": score,
            "has_personality_profile": state.get("has_personality_profile", False),
            "has_temporal": state.get("has_temporal", False),
            "entity": state.get("entity", {}),
        }

        logger.info(f"Extracted DMN context data: {data}")
        return data

    def _call_dmn_confidence_score_api(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """
        Calls the external DMN service.

        Args:
            payload: Input data for the API.

        Returns:
            JSON response from API or None on failure.
        """
        try:
            logger.info(f"Calling DMN API at {self.api_endpoint}")
            response = requests.post(self.api_endpoint, json=payload, timeout=5)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            traceback.print_exc()
            logger.error(f"Failed to call DMN API: {e}")
            return None

    def _handle_confidence_score_answer_hint(self, hint: str, context: ToolContext) -> None:
        """
        Process answer hint by substituting template parameters.
        Stores the result in `context.state['confidence_score_answer_guidance']`.

        Args:
            hint: The answer hint string with potentials placeholders (e.g. {user_pronoun})
            context: The tool context to retrieve params and store result.
        """
        # 1. Retrieve params from user state
        params = {}
        context_state = context.state

        for param_enum in AnswerHintTemplateParam:
            param_key = param_enum.value
            value = context_state.get(param_key)
            params[param_key] = value

        # 2. Perform template substitution
        try:
            processed_hint = hint.format(**params)
        except KeyError as e:
            # Fallback to original hint if keys are missing in params
            logger.warning(f"Missing param for hint format: {e}. Hint: {hint}")
            processed_hint = hint
        except ValueError as e:
            logger.warning(f"Invalid format string: {e}. Hint: {hint}")
            processed_hint = hint

        # 4. Update tool context
        context.state["confidence_score_answer_guidance"] = processed_hint
        logger.info(f"Processed answer_hint: {processed_hint}")

    def _handle_confidence_score_cta_hint(self, hints: list[str], context: ToolContext) -> None:
        """
        Process Call-To-Action hints.
        NOTE: Currently buttons are managed by PlaybookDMNHintProcessor only.
        This method is kept for future use but does not modify buttons.

        Args:
            hints: List of CTA hint strings.
            context: The tool context to store the result.
        """
        # NOTE: Buttons are managed by PlaybookDMNHintProcessor only.
        # This code is kept for future use when confidence_score needs to set buttons.
        # current_buttons = context.state["extra_data"].get("buttons", [])
        # if current_buttons is None:
        #     current_buttons = []
        # current_buttons.extend(hints)
        # context.state["extra_data"]["buttons"] = current_buttons
        logger.info(f"Received cta_hint (confidence score) but not modifying buttons: {hints}")

    def _handle_confidence_score_action_hint(self, hints: list[dict], context: ToolContext) -> None:
        """
        Process action hints by parsing JSON, filtering for instructions,
        and substituting template parameters.
        Stores the result in `context.state['confidence_score_action_results']`.

        Args:
            hints: List of action hint strings (JSON formatted).
            context: The tool context to retrieve params and store result.
        """
        processed_hints = []
        context_state = context.state
        context.state["confidence_score_action_results"] = None

        # 1. Reuse param extraction logic for action hints
        params = {}
        for param_enum in ActionHintTemplateParam:
            param_key = param_enum.value
            value = context_state.get(param_key)
            # Default to empty string if missing
            params[param_key] = value if value is not None else ""

        execute_functions = {
            "fallback_major_not_have_acapro": fallback_major_not_have_acapro,
        }

        for hint_data in hints:
            hint_type = hint_data.get("type")

            if hint_type == "instruction":
                prompt = hint_data.get("prompt", "")
                if not prompt:
                    continue
                try:
                    processed_prompt = prompt.format(**params)
                except (KeyError, ValueError) as e:
                    logger.error(f"Error formatting action hint: {e}. Prompt: {prompt}")
                    processed_prompt = prompt
                hint_data["prompt"] = processed_prompt
                processed_hints.append(processed_prompt)
                context.state["confidence_score_action_answer_guidance"] = hint_data.get("prompt")
                continue

            if hint_type == "execute":
                func_name = hint_data.get("func", "")
                if not func_name or func_name not in execute_functions:
                    logger.warning(f"Unknown or missing function in execute hint: {func_name}")
                    continue
                logger.info(f"Executing function: {func_name}")
                try:
                    results = execute_functions[func_name](context)
                except Exception as e:
                    traceback.print_exc()
                    logger.error(f"Error executing function {func_name}: {e}")
                else:
                    logger.info(f"Successfully executed function: {results}")
                    context.state["confidence_score_action_results"] = str(results)
                continue

            if hint_type == "query":
                logger.info(f"formatting action hint: {hint_data}")
                logger.error(f"Error formatting action hint: {hint_data.get('params')}")
                if hint_data.get("question"):
                    pass  # TODO

                if hint_data.get("params"):
                    params = hint_data.get("params", {})
                    params.get("is_required_media", False)
                    params.get("target_entities", [])
                    params.get("related_entities_type", [])
                    params.get("related_entities", [])
                    params.get("requested_attribute", [])
                    params.get("effective_from", None)
                    params.get("effective_to", None)
                    params.get("query_type")
                    # query_internal_data(
                    #     tool_context=context,
                    #     is_query=True,
                    #     is_required_media=is_required_media,
                    #     target_entities=target_entities,
                    #     related_entities_type=related_entities_type,
                    #     related_entities=related_entities,
                    #     requested_attribute=requested_attribute,
                    #     # effective_from=effective_from,
                    #     # effective_to=effective_to,
                    #     query_type=query_type,
                    #     original_query="",
                    #     type_hint="confidence_score"
                    # )
                    pass

        # 5. Store in state

        logger.info(f"Processed {len(processed_hints)} action hints.")
