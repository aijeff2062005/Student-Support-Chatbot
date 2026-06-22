import logging
import traceback
from pathlib import Path
from typing import Any

import requests
from google.adk.tools.tool_context import ToolContext

from configs.config_service import get_settings
from services.query_plan_execute import QueryOrchestrator

settings = get_settings()


def read_md_file(folder_path: Path, filename: str) -> str:
    if not filename.endswith(".md"):
        filename += ".md"

    file_path = folder_path / filename

    if not file_path.is_file():
        raise FileNotFoundError(f"Không tìm thấy file: {file_path}")

    return file_path.read_text(encoding="utf-8")


# from services.answer_plan

logger = logging.getLogger("why_query_plan_dmn_processing")


def _derive_potential_type(
    primary_entities: list[dict[str, Any]] | None,
    potential_entities: list[dict[str, Any]] | None,
) -> str:
    primary_entities = primary_entities or []
    potential_entities = potential_entities or []

    has_activity = any(str(entity.get("label") or "").strip().lower() == "activity" for entity in primary_entities)
    has_activity = has_activity or any(
        str(entity.get("type") or "").strip().lower() == "activity" for entity in potential_entities
    )
    if has_activity:
        return "activity"

    has_major = any(str(entity.get("label") or "").strip().lower() == "major" for entity in primary_entities)
    has_major = has_major or any(
        str(entity.get("type") or "").strip().lower() == "major" for entity in potential_entities
    )
    if has_major:
        return "major"

    return ""


# class AnswerHintTemplateParam(str, Enum):
# 	"""Enumeration for answer hint template parameters."""
#
# 	USER_PRONOUN = "user_pronoun"
# 	SELF_PRONOUN = "self_pronoun"
#
#
# class ActionHintTemplateParam(str, Enum):
# 	"""Enumeration for action hint template parameters."""
#
# 	PERSONALITY_PROFILE = "personality_profile"
# 	MAJOR = "major"
# 	USER_PRONOUN = "user_pronoun"
# 	SELF_PRONOUN = "self_pronoun"


class QueryPlanDMNProcessor:
    """Processor for fetching and handling DMN hints."""

    def __init__(self):
        """
        Initialize the DMNHintProcessor.

        Args:
            api_endpoint: The URL of the DMN hint service.
        """
        self.kogito_url = settings.kogito_endpoint_addressing_rule
        self.api_endpoint = f"{self.kogito_url}/question_query_plan"

    def process_query_plan(self, context: ToolContext) -> dict[Any, Any] | None:
        """
        Main entry point to process hints based on tool context.

        Args:
            context: The tool context containing state.

        Returns:
            The raw response from the DMN API (or empty dict on failure),
            augmented with processed hint actions.
        """
        # 1. Extract context data
        payload = self._extract_context_data(context)

        # 2. Call DMN API
        response_data = self._call_dmn_api(payload)
        if not response_data:
            return {}

        # 3. Process outputs
        output = self._apply_local_plan_override(payload, response_data.get("output", {}))
        logger.info(f"Playbook DMN output: {output}")
        if output:
            query_plan = output.get("query_plan", "")
            self._handle_query_plan(context=context, query_plan=query_plan)

            answer_plan_from_dmn = output.get("answer_plan", "")
            answer_plan = read_md_file(folder_path=settings.answer_plan_dir, filename=answer_plan_from_dmn)
            context.state.update({"answer_plan": answer_plan})
        return None

    def _extract_context_data(self, context: ToolContext) -> dict[str, Any]:
        """
        Extracts relevant fields from ToolContext state.

        Args:
            context: The tool context.

        Returns:
            Dictionary matching the DMN API input schema.
        """
        state = context.state.to_dict()
        # logger.error(f"Extracted DMN context data: {state}")
        query_agent_output = state.get("query_agent_output", {})
        logger.info(f"query_agent_output: {query_agent_output}")

        # Derive potential_type from potential_entities for DMN input
        potential_entities = state.get("potential_entities", [])
        primary_entities = state.get("primary_entities", [])
        potential_type = _derive_potential_type(primary_entities, potential_entities)

        data = {
            "qtype": state.get("qtype", "").lower(),
            "intent": state.get("intent", "").lower(),
            "topic": state.get("topic", ""),
            "subtopics": state.get("subtopics", ""),
            "compare_mode": state.get("compare_mode", False),
            "time_compare": bool(state.get("time_compare")),
            "need_disambig": state.get("need_disambig", state.get("needs_disambiguation", False)),
            "potential_type": potential_type,
        }

        # logger.error(f"Extracted DMN context data: {data}")
        return data

    def _call_dmn_api(self, payload: dict[str, Any]) -> dict[str, Any] | None:
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
            logger.info(f"Playbook response {response.text}")
            return response.json()
        except requests.RequestException as e:
            traceback.print_exc()
            logger.error(f"Failed to call DMN API: {e}")
            return None

    def _handle_query_plan(self, context: ToolContext, query_plan: str) -> None:
        """
        Process answer hint by substituting template parameters and executing func(...)
        Stores result in `context.state['playbook_answer_guidance']`
        """

        context.state.to_dict()

        orchestrator = QueryOrchestrator()
        orchestrator.process_question(tool_context=context, query_plan=query_plan)

    @staticmethod
    def _load_answer_plan(answer_plan_file: str) -> str:
        if not answer_plan_file:
            return ""
        try:
            return read_md_file(folder_path=settings.answer_plan_dir, filename=answer_plan_file)
        except Exception as e:
            logger.error(f"[MIXED] Failed to load answer_plan file '{answer_plan_file}': {e}")
            return ""

    # Each sub-query in a MIXED question gets its own isolated override_state dict
    # so threads don't race on tool_context.state.

    def process_query_plan_with_override(self, override_state: dict) -> dict:
        """
        Process a single sub-query using an isolated override_state dict.
        Does NOT read from or write to tool_context.state.
        Returns a result dict that the caller (callback) will merge into multi_query_pairs.

        Args:
            override_state: A snapshot dict of the relevant state fields for this sub-query.

        Returns:
            dict with keys: original_query, question_type, query_results, answer_plan,
                            data_crawled, data_lv2_*, confidence_score, etc.
        """
        try:
            payload = self._extract_from_override_state(override_state)
            response_data = self._call_dmn_api(payload)

            if not response_data:
                logger.warning("[MIXED] DMN returned empty for override_state")
                return self._empty_pair(override_state)

            output = self._apply_local_plan_override(payload, response_data.get("output", {}))
            logger.info(f"[MIXED] DMN output: {output}")

            query_plan = output.get("query_plan", "")
            answer_plan_file = output.get("answer_plan", "")
            answer_plan = self._load_answer_plan(answer_plan_file)

            # Run orchestrator with override_state
            orchestrator = QueryOrchestrator()
            query_result_dict = orchestrator.process_question_with_override(
                override_state=override_state, query_plan=query_plan
            )

            return {
                "original_query": override_state.get("original_query", ""),
                "question_type": override_state.get("qtype", ""),
                "query_results": query_result_dict.get("query_results"),
                "answer_plan": answer_plan,
                "data_crawled": query_result_dict.get("data_crawled"),
                "data_lv2_data_enriched_nodes": query_result_dict.get("data_lv2_data_enriched_nodes"),
                "data_lv2_siblings_relative": query_result_dict.get("data_lv2_siblings_relative"),
                "data_lv2_siblings_similar": query_result_dict.get("data_lv2_siblings_similar"),
                "data_lv2_data_enriched_relation": query_result_dict.get("data_lv2_data_enriched_relation"),
                "data_lv2_related_nodes": query_result_dict.get("data_lv2_related_nodes"),
                "confidence_score": query_result_dict.get("confidence_score"),
                "confidence_score_action_results": query_result_dict.get("confidence_score_action_results"),
                "confidence_score_answer_guidance": query_result_dict.get("confidence_score_answer_guidance"),
            }

        except Exception as e:
            logger.error(f"[MIXED] Error in process_query_plan_with_override: {e}")
            traceback.print_exc()
            return self._empty_pair(override_state)

    def _extract_from_override_state(self, override_state: dict) -> dict:
        """Extract DMN payload fields from an override_state dict (mirrors _extract_context_data)."""
        potential_entities = override_state.get("potential_entities", [])
        primary_entities = override_state.get("primary_entities", [])
        potential_type = _derive_potential_type(primary_entities, potential_entities)

        return {
            "qtype": override_state.get("qtype", "").lower(),
            "intent": override_state.get("intent", "").lower(),
            "topic": override_state.get("topic", ""),
            "subtopics": override_state.get("subtopics", ""),
            "compare_mode": override_state.get("compare_mode", False),
            "time_compare": bool(override_state.get("time_compare")),
            "need_disambig": override_state.get("need_disambig", override_state.get("needs_disambiguation", False)),
            "potential_type": potential_type,
        }

    @staticmethod
    def _normalize_subtopics(raw_subtopics: Any) -> set[str]:
        if raw_subtopics is None:
            return set()
        if isinstance(raw_subtopics, str):
            return {part.strip().lower() for part in raw_subtopics.replace(",", " ").split() if part.strip()}
        if isinstance(raw_subtopics, (list, tuple, set)):
            return {str(part).strip().lower() for part in raw_subtopics if str(part).strip()}
        return set()

    def _apply_local_plan_override(self, payload: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
        """Apply local routing patches that mirror DMN rules not yet deployed."""
        if not output:
            return output

        subtopics = self._normalize_subtopics(payload.get("subtopics"))
        topic = str(payload.get("topic") or "").lower()
        potential_type = str(payload.get("potential_type") or "").lower()
        support_tags = {
            "support",
            "academic_policy",
            "scholarship",
            "tuition",
            "payment",
            "course_registration",
            "student_document",
            "graduation",
            "major_transfer",
            "internship",
            "event",
            "activity",
            "club",
            "service",
            "department_support",
            "discipline",
            "reward",
        }

        qtype = str(payload.get("qtype") or "").lower()
        intent = str(payload.get("intent") or "").lower()
        is_support_domain = bool(subtopics & support_tags) or topic in {"policy", "fee", "student_life", "career"}
        is_support_list_domain = bool(subtopics & support_tags) or topic in {"fee", "student_life", "career"}

        if qtype == "how" and intent in {"procedure", "explain"} and "internship" in subtopics:
            patched = dict(output)
            patched["query_plan"] = "how_internship"
            patched["answer_plan"] = "how_internship"
            logger.info(
                "Applied local DMN override: qtype=%s intent=%s topic=%s subtopics=%s -> how_internship",
                payload.get("qtype"),
                payload.get("intent"),
                payload.get("topic"),
                payload.get("subtopics"),
            )
            return patched

        if (
            qtype == "how"
            and intent in {"procedure", "explain"}
            and topic == "student_life"
            and potential_type == "activity"
            and bool(subtopics & {"activity", "event"})
        ):
            patched = dict(output)
            patched["query_plan"] = "how_activity"
            patched["answer_plan"] = "how_activity"
            logger.info(
                "Applied local DMN override: qtype=%s intent=%s topic=%s subtopics=%s potential_type=%s -> how_activity",
                payload.get("qtype"),
                payload.get("intent"),
                payload.get("topic"),
                payload.get("subtopics"),
                payload.get("potential_type"),
            )
            return patched

        if qtype == "what" and intent in {"list", "count"} and is_support_list_domain:
            patched = dict(output)
            patched["query_plan"] = "what_support_list"
            patched["answer_plan"] = "what_support_list"
            logger.info(
                "Applied local DMN override: qtype=%s intent=%s topic=%s subtopics=%s -> what_support_list",
                payload.get("qtype"),
                payload.get("intent"),
                payload.get("topic"),
                payload.get("subtopics"),
            )
            return patched

        if not (qtype == "what" and intent == "relation" and is_support_domain):
            return output

        patched = dict(output)
        patched["query_plan"] = "what_support_relation"
        patched["answer_plan"] = "what_support_relation"
        logger.info(
            "Applied local DMN override: qtype=%s intent=%s topic=%s subtopics=%s -> what_support_relation",
            payload.get("qtype"),
            payload.get("intent"),
            payload.get("topic"),
            payload.get("subtopics"),
        )
        return patched

    @staticmethod
    def _empty_pair(override_state: dict) -> dict:
        """Return an empty pair dict on failure."""
        return {
            "original_query": override_state.get("original_query", ""),
            "question_type": override_state.get("qtype", ""),
            "query_results": None,
            "answer_plan": "",
            "data_crawled": None,
            "data_lv2_data_enriched_nodes": None,
            "data_lv2_siblings_relative": None,
            "data_lv2_siblings_similar": None,
            "data_lv2_data_enriched_relation": None,
            "data_lv2_related_nodes": None,
            "confidence_score": None,
            "confidence_score_action_results": None,
            "confidence_score_answer_guidance": None,
        }
