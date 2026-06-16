import traceback
from datetime import datetime
from typing import Any, cast

from google.adk.tools import ToolContext

from dbs.graph_search_helpers import _normalize_temporal_params, sanitize_neo4j_types
from dbs.keyword_search_milvus_helper import util
from dbs.milvus_helper import get_node_name_by_id, insert_crawled_data, search_crawled_data
from schemas.how_query_results import (
    HowAnswer,
    HowAnswerKind,
    HowEntities,
    HowEntityRef,
    HowIntent,
    HowMeta,
    HowQueryResult,
    HowSource,
    HowStatus,
)
from schemas.query_plan_config import (
    ExtractedEntity,
    FlowConfig,
    IntentClassification,
    MultiEntityQueryResult,
    QueryFlowResult,
    ResolvedEntity,
)
from schemas.what_query_results import (
    WhatAnswer,
    WhatAnswerKind,
    WhatEntities,
    WhatEntityRef,
    WhatIntent,
    WhatMeta,
    WhatPagination,
    WhatQueryResult,
    WhatSource,
    WhatStatus,
)
from schemas.why_query_results import (
    WhyAnswer,
    WhyAnswerKind,
    WhyEntities,
    WhyEntityRef,
    WhyIntent,
    WhyMeta,
    WhyQueryResult,
    WhySource,
    WhyStatus,
)

# from services.intent_classifier import get_intent_classifier, IntentClassifier
from services.entity_resolver import EntityResolver, get_entity_resolver
from services.function_flow_executor import FunctionFlowExecutor
from services.graph_query_builder import GraphQueryBuilder, get_graph_query_builder
from tools.reasoning_processing import NodeReasoningInput, process_reasoning, process_relation_reasoning
from utils.logging_config import get_logger
from utils.parallel_web_search import run_parallel_web_search_sync
from utils.prepare_data_for_dmn_reasoning import prepare_reasoning_input, prepare_relation_enrich_input

logger = get_logger(__name__)


class _OverrideToolContext:
    """Minimal ToolContext-like carrier for mixed-mode reasoning results."""

    def __init__(self, state: dict | None = None):
        self.state = state if state is not None else {}


def _build_override_tool_context(state: dict | None = None) -> _OverrideToolContext:
    """Create a lightweight context object exposing only the `state` field."""
    return _OverrideToolContext(state=state)


def remove_key_recursive(obj, key_to_remove):
    if hasattr(obj, "model_dump"):
        obj = obj.model_dump()
    elif hasattr(obj, "dict"):
        obj = obj.dict()

    if isinstance(obj, dict):
        return {k: remove_key_recursive(v, key_to_remove) for k, v in obj.items() if k != key_to_remove}
    elif isinstance(obj, list):
        return [remove_key_recursive(i, key_to_remove) for i in obj]
    else:
        return obj


# =============================================================================
# ORCHESTRATOR
# =============================================================================


class QueryOrchestrator:

    def __init__(
        self,
        entity_resolver: EntityResolver | None = None,
        graph_query_builder: GraphQueryBuilder | None = None,
    ):
        self.entity_resolver = entity_resolver or get_entity_resolver()
        self.graph_query_builder = graph_query_builder or get_graph_query_builder()

    # =========================================================================
    # UTILITY
    # =========================================================================
    def _remove_redundant_keys(self, obj, keys_to_remove=None):
        """
        Recursively strip out a set of metadata keys from an object.

        Defaults to ["id","created_at","updated_at","version","source_documents"].
        """
        if keys_to_remove is None:
            keys_to_remove = ["id", "created_at", "updated_at", "version", "source_documents"]
        if hasattr(obj, "model_dump"):
            obj = obj.model_dump()
        elif hasattr(obj, "dict"):
            obj = obj.dict()
        if isinstance(obj, dict):
            return {
                k: self._remove_redundant_keys(v, keys_to_remove) for k, v in obj.items() if k not in keys_to_remove
            }
        elif isinstance(obj, list):
            return [self._remove_redundant_keys(i, keys_to_remove) for i in obj]
        else:
            return obj

    def _canonical_what_result(
        self,
        intent: WhatIntent,
        answer_kind: WhatAnswerKind,
        entities: WhatEntities | None = None,
    ) -> WhatQueryResult:
        return WhatQueryResult(
            intent=intent,
            status=WhatStatus.EMPTY,
            entities=entities or WhatEntities(),
            answer=WhatAnswer(kind=answer_kind),
            pagination=WhatPagination(),
            meta=WhatMeta(source=WhatSource.GRAPH, used_fallback=False),
        )

    def _serialize_found_entities(self, found_list: Any) -> list[WhatEntityRef]:
        entities = []
        for item in found_list or []:
            if not isinstance(item, dict):
                continue
            entities.append(
                WhatEntityRef(
                    id=item.get("node_id"),
                    name=item.get("node_name") or item.get("name"),
                    type=item.get("node_type"),
                    description=item.get("description"),
                    score=item.get("score"),
                )
            )
        return entities

    def _serialize_input_entities(self, entity_list: Any) -> list[WhatEntityRef]:
        entities = []
        for item in entity_list or []:
            if not isinstance(item, dict):
                continue
            entities.append(
                WhatEntityRef(
                    name=item.get("text") or item.get("name"),
                    type=item.get("label") or item.get("type"),
                )
            )
        return entities

    def _sanitize_evidence_value(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {k: self._sanitize_evidence_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._sanitize_evidence_value(v) for v in value]
        return value

    def _normalize_entity_attribute_matches(self, matches: Any) -> list[dict[str, Any]]:
        normalized = []
        if not isinstance(matches, dict):
            return normalized
        for node_id, attributes in matches.items():
            normalized.append(
                {
                    "node_id": node_id,
                    "values": attributes if isinstance(attributes, dict) else {},
                }
            )
        return normalized

    def _slice_collection_items_for_current_page(self, list_results: Any) -> list[dict[str, Any]]:
        if not isinstance(list_results, dict):
            return []

        items = list_results.get("items", []) or []
        if not isinstance(items, list) or not items:
            return []

        start_index = list_results.get("start_index", 0)
        display_limit = list_results.get("display_limit", len(items))

        try:
            offset = max(int(start_index), 0)
        except (TypeError, ValueError):
            offset = 0
        try:
            limit = max(int(display_limit), 0)
        except (TypeError, ValueError):
            limit = len(items)

        if limit <= 0 or offset >= len(items):
            return []

        labels = {(row.get("labels") or [""])[0] for row in items if isinstance(row, dict)}
        has_mixed_major_spec = "Major" in labels and "Specialization" in labels

        if not has_mixed_major_spec:
            next_index = min(offset + limit, len(items))
            return items[offset:next_index]

        display_rows: list[dict[str, Any]] = []
        cursor = offset
        major_count = 0

        while cursor < len(items) and major_count < limit:
            row = items[cursor]
            if not isinstance(row, dict):
                cursor += 1
                continue

            label = (row.get("labels") or [""])[0]
            display_rows.append(row)
            cursor += 1

            if label != "Major":
                major_count += 1
                continue

            major_count += 1
            parent_major_id = row.get("id")
            parent_major_name = row.get("name")

            while cursor < len(items):
                child = items[cursor]
                if not isinstance(child, dict):
                    cursor += 1
                    continue

                child_label = (child.get("labels") or [""])[0]
                if child_label != "Specialization":
                    break

                same_parent = (parent_major_id and child.get("_parent_major_id") == parent_major_id) or (
                    parent_major_name and child.get("_parent_major") == parent_major_name
                )
                if not same_parent:
                    break

                display_rows.append(child)
                cursor += 1

        return display_rows

    def _build_collection_answer_data(
        self,
        list_results: Any,
        scope_entities: list[WhatEntityRef],
        item_type: str | None = None,
        matched_condition: dict[str, Any] | None = None,
        source_branch: str | None = None,
        count_mode: str | None = None,
        formatted_answer: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(list_results, dict):
            return {}

        current_page_items = self._slice_collection_items_for_current_page(list_results)

        answer_data: dict[str, Any] = {}
        if formatted_answer:
            answer_data["formatted_answer"] = formatted_answer

        answer_data.update(
            {
                "items": current_page_items,
                "current_primary_entity": list_results.get("current_primary_entity", {}) or {},
                "item_type": item_type,
                "scope_entity": scope_entities[0].model_dump(mode="json", exclude_none=True) if scope_entities else None,
                "total": list_results.get("total", 0),
            }
        )
        if matched_condition:
            answer_data["matched_condition"] = matched_condition
        if source_branch:
            answer_data["source_branch"] = source_branch
        if count_mode:
            answer_data["count_mode"] = count_mode
        for extra_key in ("selection_mode", "recommendation_query", "matched_keywords"):
            extra_value = list_results.get(extra_key)
            if extra_value not in (None, "", [], {}):
                answer_data[extra_key] = extra_value
        return {k: v for k, v in answer_data.items() if v is not None}

    def _normalize_what_attributes_result(self, final_state: dict[str, Any]) -> WhatQueryResult:
        entities = WhatEntities(
            primary=self._serialize_found_entities(final_state.get("primary_found_list")),
        )
        result = self._canonical_what_result(
            intent=WhatIntent.ATTRIBUTES,
            answer_kind=WhatAnswerKind.ATTRIBUTES,
            entities=entities,
        )

        entity_attributes = final_state.get("entity_attributes") or {}
        relation_attributes = final_state.get("relation_attributes") or []
        entity_fallback = final_state.get("entity_attributes_fallback") or {}
        relation_fallback = final_state.get("relation_attributes_fallback") or []
        media_attachments = final_state.get("media_attachments") or {}
        media_available = bool(media_attachments.get("available")) or bool(media_attachments.get("attachments"))
        # crawled_data = final_state.get("crawled_data_results")

        branch_name = None
        source = "graph"
        used_fallback = False
        answer_data = {
            "subject": entities.primary[0].model_dump(mode="json", exclude_none=True) if entities.primary else None,
            "attributes": [],
            "relation_attributes": [],
            "media_available": media_available,
        }

        if entity_attributes:
            branch_name = "entity_attributes"
            answer_data["attributes"] = self._normalize_entity_attribute_matches(entity_attributes)
            result.evidence.structured = {
                "branch": branch_name,
                "attributes": answer_data["attributes"],
            }
        elif relation_attributes:
            branch_name = "relation_attributes"
            answer_data["relation_attributes"] = relation_attributes
            result.evidence.structured = {
                "branch": branch_name,
                "relation_attributes": relation_attributes,
            }
        elif entity_fallback:
            branch_name = "entity_attributes_fallback"
            source = "hybrid"
            used_fallback = True
            answer_data["attributes"] = self._normalize_entity_attribute_matches(entity_fallback)
            result.evidence.structured = {
                "branch": branch_name,
                "attributes": answer_data["attributes"],
            }
        elif relation_fallback:
            branch_name = "relation_attributes_fallback"
            source = "hybrid"
            used_fallback = True
            answer_data["relation_attributes"] = relation_fallback
            result.evidence.structured = {
                "branch": branch_name,
                "relation_attributes": relation_fallback,
            }

        if media_available:
            result.evidence.media = {
                "available": True,
            }

        if branch_name or result.evidence.media.get("available"):
            result.status = WhatStatus.OK if not used_fallback else WhatStatus.FALLBACK
            result.answer.data = {k: v for k, v in answer_data.items() if v is not None}
            result.meta = WhatMeta(
                source=WhatSource(source),
                used_fallback=used_fallback,
            )
            return result

        # if crawled_data:
        #     result.status = WhatStatus.FALLBACK
        #     result.evidence.fallback = {"crawled_data": crawled_data}
        #     result.meta = WhatMeta(source=WhatSource.CRAWLED, used_fallback=True)

        return result

    def _normalize_major_spec_academic_result(self, final_state: dict[str, Any]) -> WhatQueryResult:
        entities = WhatEntities(
            primary=self._serialize_found_entities(final_state.get("primary_found_list")),
        )
        result = self._canonical_what_result(
            intent=WhatIntent.ATTRIBUTES,
            answer_kind=WhatAnswerKind.ATTRIBUTES,
            entities=entities,
        )

        bundle = final_state.get("major_spec_academic_bundle") or {}
        formatted_answer = final_state.get("formatted_answer") or bundle.get("formatted_answer")
        # crawled_data = final_state.get("crawled_data_results")

        if isinstance(bundle, dict) and bundle.get("status") == "ok":
            subject = bundle.get("major") or {}
            answer_data = {
                "formatted_answer": formatted_answer,
                "subject": {
                    "id": subject.get("id"),
                    "name": subject.get("name"),
                    "type": subject.get("type"),
                    "description": subject.get("description"),
                }
                if subject
                else None,
                "input_entity": bundle.get("input_entity"),
                "major": bundle.get("major"),
                "major_bundles": bundle.get("major_bundles") or [],
                "parent_specialization": bundle.get("parent_specialization"),
                "direct_academic_programs": bundle.get("direct_academic_programs") or [],
                "specializations": bundle.get("specializations") or [],
                "source_branch": bundle.get("source_branch"),
                "route": bundle.get("route"),
                "summary": bundle.get("summary") or {},
                "attributes": [
                    {
                        "node_id": subject.get("id"),
                        "values": subject.get("attributes") or {},
                    }
                ]
                if subject.get("attributes")
                else [],
            }
            result.status = WhatStatus.OK
            result.formatted_answer = formatted_answer
            result.answer.data = {k: v for k, v in answer_data.items() if v not in (None, "", [], {})}
            result.evidence.structured = {"major_spec_academic_bundle": bundle}
            result.meta = WhatMeta(source=WhatSource.GRAPH, used_fallback=False)
            return result

        if isinstance(bundle, dict) and bundle.get("status") in {"needs_disambiguation", "major_scope_not_found"}:
            answer_data = {
                "formatted_answer": formatted_answer,
                "candidate_major_names": bundle.get("candidate_major_names") or [],
                "route": bundle.get("route"),
                "summary": bundle.get("summary") or {},
            }
            result.status = WhatStatus.OK
            result.formatted_answer = formatted_answer
            result.answer.data = {k: v for k, v in answer_data.items() if v not in (None, "", [], {})}
            result.evidence.structured = {"major_spec_academic_bundle": bundle}
            result.meta = WhatMeta(source=WhatSource.GRAPH, used_fallback=False)
            return result

        # if crawled_data:
        #     result.status = WhatStatus.FALLBACK
        #     result.evidence.fallback = {"crawled_data": crawled_data}
        #     result.meta = WhatMeta(source=WhatSource.CRAWLED, used_fallback=True)

        return result

    def _normalize_what_compare_result(self, final_state: dict[str, Any]) -> WhatQueryResult:
        entities = WhatEntities(
            primary=self._serialize_found_entities(final_state.get("primary_found_list")),
            compare=self._serialize_found_entities(final_state.get("compare_found_list")),
        )
        result = self._canonical_what_result(
            intent=WhatIntent.COMPARE,
            answer_kind=WhatAnswerKind.COMPARISON,
            entities=entities,
        )

        compare_results = final_state.get("compare_results") or []
        primary_fallback_rows = final_state.get("primary_academic_program_rows") or []
        compare_fallback_rows = final_state.get("compare_academic_program_rows") or []
        primary_fallback = final_state.get("primary_academic_program_attributes") or {}
        compare_fallback = final_state.get("compare_academic_program_attributes") or {}
        # crawled_data = final_state.get("crawled_data_results")

        base_entity = entities.primary[0].model_dump(mode="json", exclude_none=True) if entities.primary else None
        compare_entities = [entity.model_dump(mode="json", exclude_none=True) for entity in entities.compare]
        answer_data = {
            "base_entity": base_entity,
            "compare_entities": compare_entities,
            "left_entity": base_entity,
            "right_entity": compare_entities[0] if compare_entities else None,
            "compared_attributes": [],
            "summary_basis": None,
        }

        if compare_results:
            answer_data["compared_attributes"] = [
                {
                    "entity": {
                        "id": item.get("node_id"),
                        "name": item.get("name"),
                        "type": (item.get("labels") or [None])[0],
                        "description": item.get("description"),
                    },
                    "attributes": item.get("properties", {}),
                }
                for item in compare_results
                if isinstance(item, dict)
            ]
            answer_data["summary_basis"] = "direct_comparison"
            result.status = WhatStatus.OK
            result.answer.data = {k: v for k, v in answer_data.items() if v is not None}
            result.evidence.structured = {
                "branch": "compare_results",
                "comparison_rows": compare_results,
            }
            result.meta = WhatMeta(source=WhatSource.GRAPH, used_fallback=False)
            return result

        if not primary_fallback_rows and primary_fallback:
            primary_fallback_rows = [
                {
                    "entity": base_entity,
                    "attributes": self._normalize_entity_attribute_matches(primary_fallback),
                }
            ]
        if not compare_fallback_rows and compare_fallback:
            compare_fallback_rows = [
                {
                    "entity": compare_entities[0] if compare_entities else None,
                    "attributes": self._normalize_entity_attribute_matches(compare_fallback),
                }
            ]

        if primary_fallback_rows or compare_fallback_rows:
            fallback_rows = [*primary_fallback_rows, *compare_fallback_rows]
            answer_data["compared_attributes"] = fallback_rows
            answer_data["summary_basis"] = "academic_program_fallback"
            result.status = WhatStatus.FALLBACK
            result.answer.data = {k: v for k, v in answer_data.items() if v is not None}
            result.evidence.structured = {
                "branch": "academic_program_fallback",
                "primary": primary_fallback_rows,
                "compare": compare_fallback_rows,
            }
            result.meta = WhatMeta(source=WhatSource.HYBRID, used_fallback=True)
            return result

        # if crawled_data:
        #     result.status = WhatStatus.FALLBACK
        #     result.evidence.fallback = {"crawled_data": crawled_data}
        #     result.meta = WhatMeta(source=WhatSource.CRAWLED, used_fallback=True)

        return result

    def _normalize_what_relation_result(self, final_state: dict[str, Any]) -> WhatQueryResult:
        entities = WhatEntities(
            primary=self._serialize_found_entities(final_state.get("primary_found_list")),
            context=self._serialize_found_entities(final_state.get("context_found_list")),
            compare=self._serialize_found_entities(final_state.get("compare_found_list")),
        )
        result = self._canonical_what_result(
            intent=WhatIntent.RELATION,
            answer_kind=WhatAnswerKind.RELATION,
            entities=entities,
        )

        relation_context = final_state.get("relation_context") or {}
        crawled_data = final_state.get("crawled_data_results")

        if relation_context:
            candidates = relation_context.get("candidates", []) or []
            relation_answer_data = final_state.get("relation_answer_data") or {}
            relation_types = []
            seen_relation_types = set()
            for candidate in candidates:
                for relationship in candidate.get("relationships", []) or []:
                    rel_type = relationship.get("type")
                    if rel_type and rel_type not in seen_relation_types:
                        seen_relation_types.add(rel_type)
                        relation_types.append(rel_type)
            answer_relation_types = relation_answer_data.get("relations") or relation_types

            result.status = WhatStatus.OK
            result.answer.data = {
                **relation_answer_data,
                "subject": (
                    entities.context[0].model_dump(mode="json", exclude_none=True)
                    if entities.context
                    else relation_context.get("context")
                ),
                "object": entities.primary[0].model_dump(mode="json", exclude_none=True) if entities.primary else None,
                "relations": answer_relation_types,
                "candidates": candidates,
            }
            result.evidence.structured = {
                "context": relation_context.get("context"),
                "total_candidates": relation_context.get("total_candidates", len(candidates)),
                "candidates": candidates,
            }
            if relation_answer_data.get("formatted_answer"):
                result.evidence.structured["formatted_answer"] = relation_answer_data["formatted_answer"]
            result.meta = WhatMeta(source=WhatSource.GRAPH, used_fallback=False)
            return result

        if crawled_data:
            result.status = WhatStatus.FALLBACK
            result.evidence.fallback = {"crawled_data": crawled_data}
            result.meta = WhatMeta(source=WhatSource.CRAWLED, used_fallback=True)

        return result

    def _normalize_what_list_like_result(
        self,
        final_state: dict[str, Any],
        input_data: dict[str, Any],
        intent: str,
        source_branch: str | None = None,
    ) -> WhatQueryResult:
        context_entities = self._serialize_found_entities(final_state.get("context_found_list"))
        if not context_entities:
            context_entities = self._serialize_input_entities(input_data.get("context_entities"))

        entities = WhatEntities(
            primary=self._serialize_found_entities(final_state.get("primary_found_list")),
            context=context_entities,
        )
        result = self._canonical_what_result(
            intent=WhatIntent(intent),
            answer_kind=WhatAnswerKind.COLLECTION,
            entities=entities,
        )

        list_results = final_state.get("list_results") or {}
        # logger.info(f"_normalize_what_list_like_result: list_results: {list_results}")
        formatted_answer = final_state.get("formatted_answer")
        crawled_data = final_state.get("crawled_data_results")
        item_types = input_data.get("count_enumerate_targets") or []
        item_type = item_types[0] if item_types else None

        if list_results:
            branch_source = source_branch or list_results.get("source")
            fallback_sources = {
                "candidate_pool_fallback",
                "temporal_fallback",
                "current_description_fallback",
                "Current description fallback",
                "admission_score_policy_method_temporal_fallback",
            }
            hybrid_sources = {
                "candidate_pool_fallback",
                "career_recommendation",
            }
            matched_condition = None
            if intent == "constraint-list":
                matched_condition = {
                    "keywords": input_data.get("keywords", []),
                    "keyword_attributes": input_data.get("keyword_attributes", []),
                }

            count_mode = None
            if intent == "count":
                count_mode = (
                    "filtered" if input_data.get("keywords") or input_data.get("primary_entities") else "simple"
                )

            result.status = WhatStatus.FALLBACK if branch_source in fallback_sources else WhatStatus.OK
            result.answer.data = self._build_collection_answer_data(
                list_results=list_results,
                scope_entities=entities.context,
                item_type=item_type,
                matched_condition=matched_condition,
                source_branch=branch_source if intent == "constraint-list" else None,
                count_mode=count_mode,
                formatted_answer=formatted_answer if intent in {"list", "count", "constraint-list"} else None,
            )
            current_page_items = self._slice_collection_items_for_current_page(list_results)
            result.evidence.structured = {
                "items": current_page_items,
                "intermediate_nodes": list_results.get("intermediate_nodes", []),
            }
            if intent == "constraint-list" and formatted_answer:
                result.evidence.structured["formatted_answer"] = formatted_answer
            result.pagination = WhatPagination(
                total=list_results.get("total", 0),
                display_limit=list_results.get("display_limit", 0),
                next_start_index=list_results.get("list_last_index", 0),
            )
            result.meta = WhatMeta(
                source=WhatSource.HYBRID if branch_source in hybrid_sources else WhatSource.GRAPH,
                used_fallback=branch_source in fallback_sources,
            )
            return result

        if crawled_data:
            result.status = WhatStatus.FALLBACK
            result.evidence.fallback = {"crawled_data": crawled_data}
            result.meta = WhatMeta(source=WhatSource.CRAWLED, used_fallback=True)

        return result

    def _normalize_what_query_results(
        self,
        query_plan: str,
        input_data: dict[str, Any],
        final_state: dict[str, Any],
    ) -> dict[str, Any]:
        flow_name = query_plan[:-5] if query_plan.endswith(".yaml") else query_plan

        if flow_name == "what_ops_major_spec_academic":
            normalized = self._normalize_major_spec_academic_result(final_state)
        elif flow_name in {"what_attributes", "what_support_attribute"}:
            normalized = self._normalize_what_attributes_result(final_state)
        elif flow_name in {"what_compare", "what_major_compare"}:
            normalized = self._normalize_what_compare_result(final_state)
        elif flow_name in {"what_relation", "what_relation_compare", "what_relation_compare_time", "what_support_relation"}:
            normalized = self._normalize_what_relation_result(final_state)
        elif flow_name in {"what_constraint_list", "what_score_constraint_list", "what_major_constraint_list"}:
            normalized = self._normalize_what_list_like_result(
                final_state=final_state,
                input_data=input_data,
                intent="constraint-list",
                source_branch=(final_state.get("list_results") or {}).get("source"),
            )
        elif flow_name in {"what_list", "what_support_list"}:
            list_intent = "count" if input_data.get("intent") == "count" else "list"
            normalized = self._normalize_what_list_like_result(
                final_state=final_state,
                input_data=input_data,
                intent=list_intent,
            )
        elif flow_name == "what_major_career_recommendation":
            normalized = self._normalize_what_list_like_result(
                final_state=final_state,
                input_data=input_data,
                intent="list",
                source_branch=(final_state.get("list_results") or {}).get("source"),
            )
        else:
            normalized = self._canonical_what_result(
                intent=WhatIntent.UNKNOWN,
                answer_kind=WhatAnswerKind.COLLECTION,
            )

        answer_data = normalized.answer.data if normalized.answer else {}
        if isinstance(answer_data, dict) and answer_data.get("formatted_answer"):
            normalized.formatted_answer = answer_data["formatted_answer"]

        return self._sanitize_evidence_value(normalized.model_dump(mode="json", exclude_none=True))

    def _canonical_how_result(
        self,
        intent: HowIntent,
        answer_kind: HowAnswerKind,
        entities: HowEntities | None = None,
    ) -> HowQueryResult:
        return HowQueryResult(
            intent=intent,
            status=HowStatus.EMPTY,
            entities=entities or HowEntities(),
            answer=HowAnswer(kind=answer_kind),
            meta=HowMeta(source=HowSource.GRAPH, used_fallback=False),
        )

    def _extract_how_step_rows(self, all_flow_results: dict[str, Any], step_name: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for flow_result in (all_flow_results or {}).values():
            if not isinstance(flow_result, dict):
                continue
            traversal_results = flow_result.get("traversal_results") or {}
            if not isinstance(traversal_results, dict):
                continue
            step_result = traversal_results.get(step_name) or {}
            if not isinstance(step_result, dict):
                continue
            step_data = step_result.get("data") or []
            if isinstance(step_data, list):
                rows.extend(item for item in step_data if isinstance(item, dict))
        return rows

    def _serialize_how_entities(self, all_flow_results: dict[str, Any]) -> HowEntities:
        primary_entities: list[HowEntityRef] = []
        for flow_result in (all_flow_results or {}).values():
            if not isinstance(flow_result, dict):
                continue
            resolved_entity = flow_result.get("resolved_entity") or {}
            if not isinstance(resolved_entity, dict):
                continue
            primary_entities.append(
                HowEntityRef(
                    id=resolved_entity.get("entity_id"),
                    name=resolved_entity.get("entity_name"),
                    type=resolved_entity.get("entity_type"),
                    description=resolved_entity.get("description"),
                    score=resolved_entity.get("similarity_score"),
                )
            )
        return HowEntities(primary=primary_entities)

    def _normalize_how_flow_name(self, query_plan: str) -> str:
        flow_name = query_plan[:-5] if query_plan.endswith(".yaml") else query_plan
        if flow_name.startswith("how_keyword_"):
            return flow_name.replace("how_keyword_", "how_", 1)
        return flow_name

    def _resolve_how_contract(self, flow_name: str) -> tuple[HowIntent, HowAnswerKind]:
        mapping = {
            "how_admission": (HowIntent.ADMISSION, HowAnswerKind.PROCEDURE),
            "how_course": (HowIntent.COURSE, HowAnswerKind.ROADMAP),
            "how_course_roadmap": (HowIntent.COURSE, HowAnswerKind.ROADMAP),
            "how_fee": (HowIntent.FEE, HowAnswerKind.PROCEDURE),
            "how_procedure_policy": (HowIntent.POLICY, HowAnswerKind.PROCEDURE),
            "how_student_life": (HowIntent.STUDENT_LIFE, HowAnswerKind.PROCEDURE),
            "how_facilities": (HowIntent.FACILITIES, HowAnswerKind.PROCEDURE),
            "how_campus_contact": (HowIntent.CAMPUS_CONTACT, HowAnswerKind.CONTACT),
            "how_skill_training": (HowIntent.SKILL_TRAINING, HowAnswerKind.ROADMAP),
            "how_skill_to_major": (HowIntent.SKILL_TO_MAJOR, HowAnswerKind.ELIGIBILITY),
            "how_major_guidance": (HowIntent.MAJOR_GUIDANCE, HowAnswerKind.GUIDANCE),
            "how_major_to_career_path": (HowIntent.MAJOR_TO_CAREER_PATH, HowAnswerKind.ROADMAP),
            "how_career_position": (HowIntent.CAREER_POSITION, HowAnswerKind.ROADMAP),
        }
        return mapping.get(flow_name, (HowIntent.UNKNOWN, HowAnswerKind.GUIDANCE))

    def _is_non_empty_how_value(self, value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return value.strip() != ""
        if isinstance(value, list):
            return any(self._is_non_empty_how_value(item) for item in value)
        if isinstance(value, dict):
            return any(self._is_non_empty_how_value(item) for item in value.values())
        return True

    def _normalize_how_query_results(
        self,
        query_plan: str,
        all_flow_results: dict[str, Any],
        crawled_data: Any | None = None,
    ) -> dict[str, Any]:
        flow_name = self._normalize_how_flow_name(query_plan)
        intent, answer_kind = self._resolve_how_contract(flow_name)
        entities = self._serialize_how_entities(all_flow_results)
        result = self._canonical_how_result(intent=intent, answer_kind=answer_kind, entities=entities)

        scope_entity = entities.primary[0].model_dump(mode="json", exclude_none=True) if entities.primary else None
        answer_data: dict[str, Any] = {}
        structured_evidence: dict[str, Any] = {}

        if flow_name == "how_admission":
            policy_rows = self._extract_how_step_rows(all_flow_results, "admission_policy")
            methods = self._extract_how_step_rows(all_flow_results, "admission_methods")
            combinations = self._extract_how_step_rows(all_flow_results, "admission_combinations")
            competitiveness = self._extract_how_step_rows(all_flow_results, "major_quotas_scores")
            answer_data = {
                "scope_entity": scope_entity,
                "policy": policy_rows[0] if policy_rows else None,
                "methods": methods,
                "combinations": combinations,
                "competitiveness": competitiveness,
            }
            structured_evidence = {
                "admission_policy": policy_rows,
                "admission_methods": methods,
                "admission_combinations": combinations,
                "major_quotas_scores": competitiveness,
            }
        elif flow_name == "how_course":
            course_profile = self._extract_how_step_rows(all_flow_results, "course_details")
            prerequisites = self._extract_how_step_rows(all_flow_results, "prerequisites")
            learning_outcomes = self._extract_how_step_rows(all_flow_results, "course_outcomes")
            skills = self._extract_how_step_rows(all_flow_results, "skills_taught")
            answer_data = {
                "scope_entity": scope_entity,
                "course_profile": course_profile[0] if course_profile else None,
                "prerequisites": prerequisites,
                "learning_outcomes": learning_outcomes,
                "skills": skills,
            }
            structured_evidence = {
                "course_details": course_profile,
                "prerequisites": prerequisites,
                "course_outcomes": learning_outcomes,
                "skills_taught": skills,
            }
        elif flow_name == "how_fee":
            tuition_policy = self._extract_how_step_rows(all_flow_results, "tuition_policy")
            scholarship_policy = self._extract_how_step_rows(all_flow_results, "scholarship_policy")
            fee_references = self._extract_how_step_rows(all_flow_results, "admission_fee_info")
            answer_data = {
                "scope_entity": scope_entity,
                "tuition_policy": tuition_policy,
                "scholarship_policy": scholarship_policy,
                "fee_references": fee_references,
            }
            structured_evidence = {
                "tuition_policy": tuition_policy,
                "scholarship_policy": scholarship_policy,
                "admission_fee_info": fee_references,
            }
        elif flow_name == "how_procedure_policy":
            policy_overview = self._extract_how_step_rows(all_flow_results, "policy_overview")
            policy_compliance = self._extract_how_step_rows(all_flow_results, "policy_compliance")
            policy_process = self._extract_how_step_rows(all_flow_results, "policy_process")
            policy_penalty = self._extract_how_step_rows(all_flow_results, "policy_penalty")
            answer_data = {
                "scope_entity": scope_entity,
                "policy": policy_overview[0] if policy_overview else None,
                "compliance": policy_compliance[0] if policy_compliance else None,
                "process": policy_process[0] if policy_process else None,
                "penalty": policy_penalty[0] if policy_penalty else None,
            }
            structured_evidence = {
                "policy_overview": policy_overview,
                "policy_compliance": policy_compliance,
                "policy_process": policy_process,
                "policy_penalty": policy_penalty,
            }
        elif flow_name == "how_student_life":
            clubs = self._extract_how_step_rows(all_flow_results, "clubs_overview")
            activities = self._extract_how_step_rows(all_flow_results, "activities")
            support_services = self._extract_how_step_rows(all_flow_results, "support_services_detail")
            answer_data = {
                "scope_entity": scope_entity,
                "clubs": clubs,
                "activities": activities,
                "support_services": support_services,
            }
            structured_evidence = {
                "clubs_overview": clubs,
                "activities": activities,
                "support_services_detail": support_services,
            }
        elif flow_name == "how_facilities":
            facility_usage = self._extract_how_step_rows(all_flow_results, "facility_usage")
            manager_contacts = self._extract_how_step_rows(all_flow_results, "managed_facilities")
            answer_data = {
                "scope_entity": scope_entity,
                "facility_usage": facility_usage,
                "manager_contacts": manager_contacts,
            }
            structured_evidence = {
                "facility_usage": facility_usage,
                "managed_facilities": manager_contacts,
            }
        elif flow_name == "how_campus_contact":
            department_contacts = self._extract_how_step_rows(all_flow_results, "department_contacts")
            campus_locations = self._extract_how_step_rows(all_flow_results, "campus_locations")
            university_contacts = self._extract_how_step_rows(all_flow_results, "university_contacts")
            answer_data = {
                "scope_entity": scope_entity,
                "department_contacts": department_contacts,
                "campus_locations": campus_locations,
                "university_contacts": university_contacts,
            }
            structured_evidence = {
                "department_contacts": department_contacts,
                "campus_locations": campus_locations,
                "university_contacts": university_contacts,
            }
        elif flow_name == "how_skill_training":
            skill_profile = self._extract_how_step_rows(all_flow_results, "skill_info")
            training_courses = self._extract_how_step_rows(all_flow_results, "courses_teaching_skill")
            career_applications = self._extract_how_step_rows(all_flow_results, "career_positions_using_skill")
            answer_data = {
                "scope_entity": scope_entity,
                "skill_profile": skill_profile[0] if skill_profile else None,
                "training_courses": training_courses,
                "career_applications": career_applications,
            }
            structured_evidence = {
                "skill_info": skill_profile,
                "courses_teaching_skill": training_courses,
                "career_positions_using_skill": career_applications,
            }
        elif flow_name == "how_skill_to_major":
            recommended_majors = self._extract_how_step_rows(all_flow_results, "related_majors")
            answer_data = {
                "scope_entity": scope_entity,
                "recommended_majors": recommended_majors,
            }
            structured_evidence = {
                "related_majors": recommended_majors,
            }
        elif flow_name == "how_major_guidance":
            major_profile = self._extract_how_step_rows(all_flow_results, "major_info")
            training_basics = self._extract_how_step_rows(all_flow_results, "education_system_duration")
            learning_outcomes = self._extract_how_step_rows(all_flow_results, "program_learning_outcomes")
            career_options = self._extract_how_step_rows(all_flow_results, "career_positions")
            required_skills = self._extract_how_step_rows(all_flow_results, "required_skills")
            trends = self._extract_how_step_rows(all_flow_results, "market_trends")
            stories = self._extract_how_step_rows(all_flow_results, "talented_stories")
            answer_data = {
                "scope_entity": scope_entity,
                "major_profile": major_profile[0] if major_profile else None,
                "training_path": {
                    "education_system_duration": training_basics[0] if training_basics else None,
                    "program_learning_outcomes": learning_outcomes,
                },
                "career_options": career_options,
                "required_skills": required_skills,
                "supporting_trends": {
                    "market_trends": trends,
                    "talented_stories": stories,
                },
            }
            structured_evidence = {
                "major_info": major_profile,
                "education_system_duration": training_basics,
                "program_learning_outcomes": learning_outcomes,
                "career_positions": career_options,
                "required_skills": required_skills,
                "market_trends": trends,
                "talented_stories": stories,
            }
        elif flow_name == "how_major_to_career_path":
            career_options = self._extract_how_step_rows(all_flow_results, "career_positions")
            progression_edges = self._extract_how_step_rows(all_flow_results, "career_progression")
            answer_data = {
                "scope_entity": scope_entity,
                "career_options": career_options,
                "progression_edges": progression_edges,
            }
            structured_evidence = {
                "career_positions": career_options,
                "career_progression": progression_edges,
            }
        elif flow_name == "how_career_position":
            profile = self._extract_how_step_rows(all_flow_results, "career_info")
            answer_data = {
                "scope_entity": scope_entity,
                "career_profile": profile[0] if profile else None,
            }
            structured_evidence = {
                "career_info": profile,
            }

        has_structured_data = self._is_non_empty_how_value(structured_evidence)

        if has_structured_data:
            result.status = HowStatus.OK
            result.answer.data = {k: v for k, v in answer_data.items() if v is not None}
            result.evidence.structured = structured_evidence
            result.meta = HowMeta(source=HowSource.GRAPH, used_fallback=False)
        elif crawled_data:
            result.status = HowStatus.FALLBACK
            result.evidence.fallback = {"crawled_data": crawled_data}
            result.meta = HowMeta(source=HowSource.CRAWLED, used_fallback=True)

        return self._sanitize_evidence_value(result.model_dump(mode="json", exclude_none=True))

    def _canonical_why_result(
        self,
        intent: WhyIntent,
        answer_kind: WhyAnswerKind,
        entities: WhyEntities | None = None,
    ) -> WhyQueryResult:
        return WhyQueryResult(
            intent=intent,
            status=WhyStatus.EMPTY,
            entities=entities or WhyEntities(),
            answer=WhyAnswer(kind=answer_kind),
            meta=WhyMeta(source=WhySource.GRAPH, used_fallback=False),
        )

    def _extract_why_step_rows(self, all_flow_results: dict[str, Any], step_name: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for flow_result in (all_flow_results or {}).values():
            if not isinstance(flow_result, dict):
                continue
            traversal_results = flow_result.get("traversal_results") or {}
            if not isinstance(traversal_results, dict):
                continue
            step_result = traversal_results.get(step_name) or {}
            if not isinstance(step_result, dict):
                continue
            step_data = step_result.get("data") or []
            if isinstance(step_data, list):
                rows.extend(item for item in step_data if isinstance(item, dict))
        return rows

    def _serialize_why_entities(self, all_flow_results: dict[str, Any]) -> WhyEntities:
        primary_entities: list[WhyEntityRef] = []
        for flow_result in (all_flow_results or {}).values():
            if not isinstance(flow_result, dict):
                continue
            resolved_entity = flow_result.get("resolved_entity") or {}
            if not isinstance(resolved_entity, dict):
                continue
            primary_entities.append(
                WhyEntityRef(
                    id=resolved_entity.get("entity_id"),
                    name=resolved_entity.get("entity_name"),
                    type=resolved_entity.get("entity_type"),
                    description=resolved_entity.get("description"),
                    score=resolved_entity.get("similarity_score"),
                )
            )
        return WhyEntities(primary=primary_entities)

    def _resolve_why_contract(self, flow_name: str) -> tuple[WhyIntent, WhyAnswerKind]:
        mapping = {
            "why_admission": (WhyIntent.ADMISSION, WhyAnswerKind.ARGUMENT),
            "why_course": (WhyIntent.COURSE, WhyAnswerKind.ARGUMENT),
            "why_explain_major": (WhyIntent.EXPLAIN_MAJOR, WhyAnswerKind.ARGUMENT),
            "why_student_life": (WhyIntent.STUDENT_LIFE, WhyAnswerKind.ARGUMENT),
            "why_faculty": (WhyIntent.FACULTY, WhyAnswerKind.ARGUMENT),
            "why_fee": (WhyIntent.FEE, WhyAnswerKind.ARGUMENT),
            "why_keyword_major": (WhyIntent.KEYWORD_MAJOR, WhyAnswerKind.ARGUMENT),
            "why_policy": (WhyIntent.POLICY, WhyAnswerKind.ARGUMENT),
            "why_program": (WhyIntent.PROGRAM, WhyAnswerKind.ARGUMENT),
            "why_university": (WhyIntent.UNIVERSITY, WhyAnswerKind.ARGUMENT),
        }
        return mapping.get(flow_name, (WhyIntent.UNKNOWN, WhyAnswerKind.ARGUMENT))

    def _is_non_empty_why_value(self, value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return value.strip() != ""
        if isinstance(value, list):
            return any(self._is_non_empty_why_value(item) for item in value)
        if isinstance(value, dict):
            return any(self._is_non_empty_why_value(item) for item in value.values())
        return True

    def _normalize_why_query_results(
        self,
        query_plan: str,
        all_flow_results: dict[str, Any],
        crawled_data: Any | None = None,
    ) -> dict[str, Any]:
        flow_name = query_plan[:-5] if query_plan.endswith(".yaml") else query_plan
        intent, answer_kind = self._resolve_why_contract(flow_name)
        entities = self._serialize_why_entities(all_flow_results)
        result = self._canonical_why_result(intent=intent, answer_kind=answer_kind, entities=entities)

        scope_entity = entities.primary[0].model_dump(mode="json", exclude_none=True) if entities.primary else None

        step_names: list[str] = []
        for flow_result in (all_flow_results or {}).values():
            if not isinstance(flow_result, dict):
                continue
            traversal_results = flow_result.get("traversal_results") or {}
            if not isinstance(traversal_results, dict):
                continue
            for step_name in traversal_results.keys():
                if isinstance(step_name, str) and step_name not in step_names:
                    step_names.append(step_name)

        topics: dict[str, Any] = {}
        structured_evidence: dict[str, Any] = {}
        for step_name in step_names:
            rows = self._extract_why_step_rows(all_flow_results, step_name)
            if not rows:
                continue
            topics[step_name] = rows
            structured_evidence[step_name] = rows

        if self._is_non_empty_why_value(structured_evidence):
            result.status = WhyStatus.OK
            result.answer.data = {
                "scope_entity": scope_entity,
                "topics": topics,
                "query_results_raw": self._sanitize_evidence_value(all_flow_results),
            }
            result.evidence.structured = structured_evidence
            result.meta = WhyMeta(source=WhySource.GRAPH, used_fallback=False)
        elif crawled_data:
            result.status = WhyStatus.FALLBACK
            result.evidence.fallback = {"crawled_data": crawled_data}
            result.meta = WhyMeta(source=WhySource.CRAWLED, used_fallback=True)

        return self._sanitize_evidence_value(result.model_dump(mode="json", exclude_none=True))

    def _to_iso_start(self, value: str | None) -> str | None:
        """Convert year-like input to ISO datetime start boundary for Neo4j datetime()."""
        if not value:
            return None
        s = str(value).strip()
        if len(s) == 4 and s.isdigit():
            return f"{s}-01-01T00:00:00Z"
        return s

    def _to_iso_end(self, value: str | None) -> str | None:
        """Convert year-like input to ISO datetime end boundary for Neo4j datetime()."""
        if not value:
            return None
        s = str(value).strip()
        if len(s) == 4 and s.isdigit():
            return f"{s}-12-31T23:59:59Z"
        return s

    def _resolve_major_for_how_admission_scope(
        self, entity: ResolvedEntity, time_condition: dict | None = None
    ) -> ResolvedEntity | None:
        """
        For how_admission only: map AcademicProgram/Specialization -> Major.
        """
        if entity.entity_type == "Major":
            return entity
        if entity.entity_type not in {"AcademicProgram", "Specialization"} or not entity.entity_id:
            return None

        effective_from, effective_to = _normalize_temporal_params(time_condition)
        if effective_from and not effective_to:
            effective_to = effective_from

        params = {
            "target_id": entity.entity_id,
            "effective_from": self._to_iso_start(effective_from),
            "effective_to": self._to_iso_end(effective_to),
        }

        cypher = """
        MATCH (target {id: $target_id})
        CALL {
          WITH target
          WITH target WHERE target:Specialization
          MATCH (m:Major)-[r_ms:INCLUDES]->(target)
          WHERE ($effective_from IS NULL OR r_ms.effective_to IS NULL OR datetime($effective_from) <= datetime(r_ms.effective_to))
            AND ($effective_to IS NULL OR r_ms.effective_from IS NULL OR datetime($effective_to) >= datetime(r_ms.effective_from))
          OPTIONAL MATCH (m)<-[r_app:APPLIES_TO]-(am:AdmissionMethod)
          WHERE ($effective_from IS NULL OR r_app.effective_to IS NULL OR datetime($effective_from) <= datetime(r_app.effective_to))
            AND ($effective_to IS NULL OR r_app.effective_from IS NULL OR datetime($effective_to) >= datetime(r_app.effective_from))
          RETURN m AS major, 1 AS route_priority, count(am) AS admission_count
          UNION
          WITH target
          WITH target WHERE target:AcademicProgram
          MATCH (m:Major)-[r_ma:INCLUDES]->(target)
          WHERE ($effective_from IS NULL OR r_ma.effective_to IS NULL OR datetime($effective_from) <= datetime(r_ma.effective_to))
            AND ($effective_to IS NULL OR r_ma.effective_from IS NULL OR datetime($effective_to) >= datetime(r_ma.effective_from))
          OPTIONAL MATCH (m)<-[r_app:APPLIES_TO]-(am:AdmissionMethod)
          WHERE ($effective_from IS NULL OR r_app.effective_to IS NULL OR datetime($effective_from) <= datetime(r_app.effective_to))
            AND ($effective_to IS NULL OR r_app.effective_from IS NULL OR datetime($effective_to) >= datetime(r_app.effective_from))
          RETURN m AS major, 1 AS route_priority, count(am) AS admission_count
          UNION
          WITH target
          WITH target WHERE target:AcademicProgram
          MATCH (m:Major)-[r_ms:INCLUDES]->(:Specialization)-[r_sa:INCLUDES]->(target)
          WHERE ($effective_from IS NULL OR r_sa.effective_to IS NULL OR datetime($effective_from) <= datetime(r_sa.effective_to))
            AND ($effective_to IS NULL OR r_sa.effective_from IS NULL OR datetime($effective_to) >= datetime(r_sa.effective_from))
            AND ($effective_from IS NULL OR r_ms.effective_to IS NULL OR datetime($effective_from) <= datetime(r_ms.effective_to))
            AND ($effective_to IS NULL OR r_ms.effective_from IS NULL OR datetime($effective_to) >= datetime(r_ms.effective_from))
          OPTIONAL MATCH (m)<-[r_app:APPLIES_TO]-(am:AdmissionMethod)
          WHERE ($effective_from IS NULL OR r_app.effective_to IS NULL OR datetime($effective_from) <= datetime(r_app.effective_to))
            AND ($effective_to IS NULL OR r_app.effective_from IS NULL OR datetime($effective_to) >= datetime(r_app.effective_from))
          RETURN m AS major, 2 AS route_priority, count(am) AS admission_count
        }
        WITH major, route_priority, admission_count
        WHERE major IS NOT NULL
        RETURN major.id AS entity_id,
               major.name AS entity_name,
               labels(major)[0] AS entity_type,
               route_priority,
               admission_count
        ORDER BY admission_count DESC, route_priority ASC, entity_name ASC
        LIMIT 1
        """

        try:
            with self.graph_query_builder._get_driver().session() as session:
                row = session.run(cypher, **params).single()
            if not row:
                return None

            return ResolvedEntity(
                entity_id=row["entity_id"],
                entity_name=row["entity_name"],
                entity_type=row["entity_type"],
                similarity_score=entity.similarity_score,
                description=entity.description,
            )
        except Exception:
            traceback.print_exc()
            return None

    # =========================================================================
    # MAIN ENTRY POINT
    # =========================================================================

    def process_question(
        self,
        tool_context: ToolContext,
        query_plan: str,
        # answer_plan: str
    ) -> None:
        try:
            state = tool_context.state.to_dict()
            # query_agent_output = state.get("query_agent_output", "")


            # =================================================================
            original_query = state.get("original_query", "")

            subtopics = state.get("subtopics", [])
            time_filter = state.get("time_filter") or state.get("time")
            if not time_filter:
                time_filter = None
            elif hasattr(time_filter, "model_dump"):
                time_filter = time_filter.model_dump()
            elif hasattr(time_filter, "dict"):
                time_filter = time_filter.dict()
            primary_entities = state.get("primary_entities", [])
            context_entities = state.get("context_entities", [])
            topic = state.get("topic", "")
            logger.info("[QueryOrchestrator] topic=%s primary_entities=%s", topic, len(primary_entities))
            if len(primary_entities) == 0 and len(context_entities) == 0 and topic == "unknown":
                logger.info("[QueryOrchestrator] no primary entities, running fallback retrieval")
                # Try potential_entities first (inferred by LLM from question content)
                potential_entities = state.get("potential_entities", [])

                # original_query = state.get("original_query", "") # Removed from here
                keyword_results = util.get_best(question=original_query)
                logger.debug(
                    "[QueryOrchestrator] keyword_results_found=%s", bool(keyword_results and keyword_results.record)
                )
                if keyword_results is not None and keyword_results.record:
                    for keyword_result in keyword_results.record:
                        node_id = keyword_result.metadata.get("node_id")
                        node_type = keyword_result.metadata.get("node_type")
                        if not isinstance(node_id, (str, int)):
                            continue
                        node_name = get_node_name_by_id(node_id)
                        if not node_name:
                            continue
                        primary_entities.append({"label": str(node_type or ""), "text": node_name})
                else:
                    # Try searching in crawled data collection first
                    crawled_results = search_crawled_data(question=original_query, top_k=1, threshold=0.71)

                    if crawled_results:
                        # Found in collection - use cached result
                        logger.info(
                            f"[QueryOrchestrator] Found in crawled data collection: {len(crawled_results)} results"
                        )
                        data_crawled = crawled_results[0]["content"]
                        tool_context.state["data_crawled"] = data_crawled
                    else:
                        # Not found in collection - perform web search and cache result
                        logger.info("[QueryOrchestrator] crawled cache miss, performing web search")
                        web_search_result = run_parallel_web_search_sync(query=original_query)

                        # Extract data from the result dictionary
                        data_crawled = web_search_result.get("summary", "")
                        source_types = web_search_result.get("source_types", [])
                        source_urls = web_search_result.get("source_urls", [])

                        # Insert the result into collection for future use
                        insert_success = insert_crawled_data(
                            question=original_query,
                            content=data_crawled,
                            source_type=source_types,
                            source_url=source_urls,
                            supporting_node_type=[],
                            status="provisional",
                        )

                        if insert_success:
                            logger.info("[QueryOrchestrator] cached web search result in collection")
                        else:
                            logger.error("[QueryOrchestrator] failed to cache web search result")

                        # tool_context.state["data_crawled"] = data_crawled
                    tool_context.state["data_crawled"] = data_crawled
                # Append potential_entities as fallback (type -> label mapping)
                for pe in potential_entities:
                    pe_type = pe.get("type", "")
                    pe_label = pe.get("label", "")
                    if pe_label and not any(e.get("text") == pe_label for e in primary_entities):
                        primary_entities.append({"label": pe_type.capitalize(), "text": pe_label})

            question_type = state.get("qtype")
            input_data = tool_context.state["input_data"]

            if question_type == "WHAT":
                logger.info("[QueryOrchestrator] question_type=WHAT, using FunctionFlowExecutor")
                executor = FunctionFlowExecutor()

                # Construct input data for the flow


                # Inject start_index for "continue listing" feature
                # If same list topic was previously queried and list_last_index exists,
                # continue from where we left off. Otherwise start at 0.
                previous_topics = state.get("count_enumerate_targets", [])
                if previous_topics is None:
                    previous_topics = []
                current_topics = input_data.get("count_enumerate_targets", [])
                if current_topics is None:
                    current_topics = []
                previous_subtopics = state.get("list_subtopics", [])
                current_subtopics = input_data.get("subtopics", [])
                previous_last_index = state.get("list_last_index", 0)
                previous_total = state.get("list_total", 0)

                if (
                    input_data.get("intent") in {"list", "constraint-list", "career_list"}
                    and set(previous_topics) == set(current_topics)
                    and set(previous_subtopics) == set(current_subtopics)
                    and 0 < previous_last_index < previous_total
                ):
                    input_data["start_index"] = previous_last_index
                    logger.info(
                        f"[QueryOrchestrator] Continue listing: start_index={previous_last_index} "
                        f"for topics '{current_topics}'"
                    )
                else:
                    input_data["start_index"] = 0

                # If LLM incorrectly puts a scope entity (Major/Specialization/Faculty)
                if input_data.get("intent") in ("count", "list", "constraint-list", "career_list"):
                    _SCOPE_LABELS = {"Major", "Specialization", "Faculty", "Department"}
                    _primary = input_data.get("primary_entities", [])
                    _context = input_data.get("context_entities", [])
                    if not input_data.get("compare_mode"):
                        # Find scope entities misplaced in primary_entities
                        misplaced = [e for e in _primary if e.get("label") in _SCOPE_LABELS]

                        if misplaced:
                            # Only swap if context is generic (empty or University-only)
                            context_is_generic = not _context or all(e.get("label") == "University" for e in _context)
                            if context_is_generic:
                                logger.info(
                                    f"[QueryOrchestrator] Defensive swap: moving {misplaced} "
                                    f"from primary_entities to context_entities"
                                )
                                input_data["context_entities"] = misplaced
                                input_data["primary_entities"] = [e for e in _primary if e not in misplaced]

                user_state = tool_context.state.get("user_state", {})
                if not isinstance(user_state, dict):
                    user_state = {}
                input_data["potential_majors"] = tool_context.state.get("potential_majors") or user_state.get(
                    "potential_majors", []
                )
                input_data["interested_majors"] = tool_context.state.get("interested_majors") or user_state.get(
                    "interested_majors", []
                )

                # Determine flow file path
                if query_plan.endswith(".yaml"):
                    flow_file_path = f"{self.graph_query_builder.config_base_path}/{query_plan}"
                else:
                    flow_file_path = f"{self.graph_query_builder.config_base_path}/{query_plan}.yaml"

                logger.info("Executing flow: %s", flow_file_path)

                # EXECUTE FLOW SYNCHRONOUSLY (Executor handles async steps internally via nest_asyncio)
                final_state = executor.execute_flow(flow_file_path, input_data)
                final_state = sanitize_neo4j_types(final_state)

                if "candidate_pool" in final_state:
                    del final_state["candidate_pool"]
                if "matched_attr_results" in final_state:
                    del final_state["matched_attr_results"]
                if "matched_rel_results" in final_state:
                    del final_state["matched_rel_results"]
                # logger.error(f"[QueryOrchestrator] final_state: {final_state}")
                intent = input_data.get("intent", "")
                subtopics = input_data.get("subtopics", [])
                media_attachments = final_state.get("media_attachments", {})
                reasoning_input_state = (
                    state.get("input_data", {}) if isinstance(state.get("input_data", {}), dict) else {}
                )
                current_year = datetime.now().year
                reasoning_time = input_data.get("time", {})
                reasoning_effective_from = reasoning_input_state.get("time", {}).get("from_year", str(current_year))
                reasoning_effective_to = reasoning_input_state.get("time", {}).get("to_year", str(current_year))
                if intent in ["attributes", "definition"]:
                    entity_attributes = final_state.get("entity_attributes", {})
                    fallback_major_attributes_to_academic_program = final_state.get(
                        "fallback_major_attributes_to_academic_program", {}
                    )
                    if entity_attributes:
                        entity_attributes = self._remove_redundant_keys(entity_attributes)
                        enrich_input = prepare_reasoning_input(
                            attribute_matches=entity_attributes,
                            effective_from=reasoning_effective_from,
                            effective_to=reasoning_effective_to,
                            time=reasoning_time,
                        )
                    elif fallback_major_attributes_to_academic_program:
                        enrich_input = prepare_reasoning_input(
                            attribute_matches=fallback_major_attributes_to_academic_program,
                            effective_from=reasoning_effective_from,
                            effective_to=reasoning_effective_to,
                            time=reasoning_time,
                        )
                        fallback_major_attributes_to_academic_program = self._remove_redundant_keys(
                            fallback_major_attributes_to_academic_program
                        )

                    else:
                        enrich_input = None

                    if enrich_input:
                        enrich_result = process_reasoning(
                            tool_context=tool_context,
                            input_data=cast(NodeReasoningInput, enrich_input),
                            enrich_top_k=5,
                            similarity_threshold=0.7,
                        )
                        logger.debug("reasoning_result generated: %s", bool(enrich_result))
                if "media_attachments" in final_state:
                    logger.debug("processing media enrichment")
                    if media_attachments.get("attachments"):
                        # Read existing extra_data (may contain buttons, etc.)
                        existing_extra_data = tool_context.state.get("extra_data", {})
                        if not isinstance(existing_extra_data, dict):
                            logger.error("Failed to parse existing extra_data: value is not a dictionary")
                            existing_extra_data = {}

                        # Merge media attachments into extra_data
                        existing_extra_data["attachments"] = media_attachments["attachments"]

                        # Save back to state
                        tool_context.state["extra_data"] = existing_extra_data
                        tool_context.state["has_media"] = bool(
                            media_attachments and media_attachments.get("attachments")
                        )

                        logger.info(f"Saved {len(media_attachments['attachments'])} media attachments to extra_data")
                    else:
                        logger.debug("No media attachments to save")
                        tool_context.state["has_media"] = False
                        current_extra_data = tool_context.state.get("extra_data", {})
                        current_extra_data["attachments"] = None
                        tool_context.state["extra_data"] = current_extra_data
                if intent == "relation":
                    relation_input = (final_state.get("subgraph", {}) or {}).get("direct_connections", {})
                    logger.debug("[QueryOrchestrator] relation_input exists=%s", bool(relation_input))
                    if relation_input:
                        logger.debug("[QueryOrchestrator] relation enrichment started")
                        relation_data_prepare = prepare_relation_enrich_input(
                            graph_data=relation_input,
                            effective_from=reasoning_effective_from,
                            effective_to=reasoning_effective_to,
                            time=reasoning_time,
                        )
                        logger.debug(
                            "[QueryOrchestrator] relation_data_prepared items=%s",
                            len(relation_data_prepare.get("items", [])),
                        )
                        relation_enrich = process_relation_reasoning(
                            tool_context=tool_context,
                            input_data=relation_data_prepare,
                            enrich_top_k=5,
                            similarity_threshold=0.7,
                        )
                        logger.debug("relation enrichment finished: %s", bool(relation_enrich))

                if "subgraph" in final_state:
                    del final_state["subgraph"]

                list_results = final_state.get("list_results", {})
                if isinstance(list_results, dict) and "list_last_index" in list_results:
                    tool_context.state["list_last_index"] = list_results.get("list_last_index", 0)
                    tool_context.state["list_total"] = list_results.get("total", 0)
                    tool_context.state["count_enumerate_targets"] = input_data.get("count_enumerate_targets", [])
                    tool_context.state["list_subtopics"] = subtopics

                normalized_query_results = self._normalize_what_query_results(
                    query_plan=query_plan,
                    input_data=input_data,
                    final_state=final_state,
                )

                tool_context.state.update(
                    {
                        "query_results": normalized_query_results,
                    }
                )
                # Save pagination state for "continue listing" feature

                # pass

            # primary_entities = ExtractedIntent()
            # time = TimeInfo(time_filter)
            else:
                logger.info("[QueryOrchestrator] processing non-WHAT flow, primary_entities=%s", len(primary_entities))
                if query_plan == "how_admission":
                    entities_for_flow = context_entities or [{"label": "University", "text": "Trường Đại học Gia Định"}]
                elif not primary_entities and not context_entities:
                    entities_for_flow = [{"label": "University", "text": "Trường Đại học Gia Định"}]
                else:
                    entities_for_flow = primary_entities if primary_entities else context_entities

                intent = IntentClassification(
                    flow_config=FlowConfig(
                        flow_file=query_plan,
                        entities_to_resolve=cast(list[ExtractedEntity], entities_for_flow),
                        subtopics=subtopics,
                        time_filter=time_filter,
                    )
                )
                logger.debug("[QueryOrchestrator] intent resolved")

                # =================================================================
                # CHECK QUESTION TYPE FOR WHAT QUERIES
                # =================================================================

                # =================================================================
                # CHECK ENTITY REQUIREMENTS
                # =================================================================

                # ... existing logic for WHY/HOW ...

                flow_file = intent.flow_config.flow_file + ".yaml"
                logger.info("[QueryOrchestrator] flow_file=%s", flow_file)
                time_condition = input_data.get("time", {})

                flow_config = self.graph_query_builder.load_flow_config(flow_file)
                logger.debug("[QueryOrchestrator] flow_config loaded")
                requires_entity_id = flow_config.get("requires_entity_id", True)
                logger.debug("[QueryOrchestrator] requires_entity_id=%s", requires_entity_id)
                # vd: [ExtractedEntity(label="Major", text="CNTT")]
                entities_to_resolve = intent.flow_config.entities_to_resolve
                logger.debug("[QueryOrchestrator] entities_to_resolve=%s", len(entities_to_resolve or []))
                # =================================================================
                # STEP 2: ENTITY RESOLUTION
                #
                # Workflow:
                #
                # =================================================================
                resolved_entities = self._resolve_entities(
                    entities_to_resolve, requires_entity_id, flow_config, time_condition
                )

                logger.info("[QueryOrchestrator] resolved_entities=%s", len(resolved_entities or []))

                # if resolved_entities is None:
                # 	return self._build_entity_not_found_response(
                # 		entities_to_resolve, intent, debug_info
                # 	)

                # =================================================================
                # STEP 3: GRAPH TRAVERSAL
                #
                # Workflow:
                #
                # =================================================================
                graph_results = self._execute_graph_traversal(
                    resolved_entities or [],
                    flow_file,
                    time_condition=time_condition,
                )

                # =================================================================
                # STEP 4: LLM RESPONSE
                #
                #
                # Output:
                # =================================================================
                graph_results = sanitize_neo4j_types(graph_results)
                graph_results = remove_key_recursive(graph_results, "cypher_query")
                graph_results = self._remove_redundant_keys(graph_results)
                logger.debug("[QueryOrchestrator] graph traversal completed")
                normalized_query_results = graph_results.get("all_flow_results")
                if query_plan.startswith("how_"):
                    normalized_query_results = self._normalize_how_query_results(
                        query_plan=query_plan,
                        all_flow_results=graph_results.get("all_flow_results") or {},
                        crawled_data=tool_context.state.get("data_crawled"),
                    )
                elif query_plan.startswith("why_"):
                    normalized_query_results = self._normalize_why_query_results(
                        query_plan=query_plan,
                        all_flow_results=graph_results.get("all_flow_results") or {},
                        crawled_data=tool_context.state.get("data_crawled"),
                    )
                tool_context.state.update(
                    {
                        "query_results": normalized_query_results,
                    }
                )
            # return await self._generate_response(
            # 	question,
            # 	request.user_profile,
            # 	intent,
            # 	resolved_entities,
            # 	graph_results,
            # 	debug_info
            # )
        except Exception:
            traceback.print_exc()

    def process_question_with_override(
        self,
        override_state: dict,
        query_plan: str,
    ) -> dict:
        """
        Mirror of process_question that reads from override_state and returns
        a result dict instead of writing to tool_context.state.
        Called from each thread in MIXED mode.

        Args:
                override_state: Isolated state snapshot for this sub-query.
                query_plan: The query plan file name (from DMN output).

        Returns:
                dict with query_results, data_crawled, data_lv2_* etc.
        """
        result = {
            "query_results": None,
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
        try:
            original_query = override_state.get("original_query", "")
            subtopics = override_state.get("subtopics", [])
            time_filter = override_state.get("time_filter") or override_state.get("time")
            if not time_filter:
                time_filter = None
            elif hasattr(time_filter, "model_dump"):
                time_filter = time_filter.model_dump()
            elif hasattr(time_filter, "dict"):
                time_filter = time_filter.dict()
            primary_entities = list(override_state.get("primary_entities", []))
            context_entities = list(override_state.get("context_entities", []))
            topic = override_state.get("topic", "")
            potential_entities = override_state.get("potential_entities", [])
            question_type = override_state.get("qtype")

            logger.info(
                f"[MIXED/Override] question_type={question_type}, topic={topic}, primary_entities={primary_entities}"
            )

            if len(primary_entities) == 0 and len(context_entities) == 0 and topic == "university":
                keyword_results = util.get_best(question=original_query)
                if keyword_results is not None and keyword_results.record:
                    for keyword_result in keyword_results.record:
                        node_id = keyword_result.metadata.get("node_id")
                        node_type = keyword_result.metadata.get("node_type")
                        if not isinstance(node_id, (str, int)):
                            continue
                        node_name = get_node_name_by_id(node_id)
                        if not node_name:
                            continue
                        primary_entities.append({"label": str(node_type or ""), "text": node_name})
                else:
                    crawled_results = search_crawled_data(question=original_query, top_k=1, threshold=0.71)
                    if crawled_results:
                        result["data_crawled"] = crawled_results[0]["content"]
                    else:
                        web_search_result = run_parallel_web_search_sync(query=original_query)
                        data_crawled = web_search_result.get("summary", "")
                        source_types = web_search_result.get("source_types", [])
                        source_urls = web_search_result.get("source_urls", [])
                        insert_crawled_data(
                            question=original_query,
                            content=data_crawled,
                            source_type=source_types,
                            source_url=source_urls,
                            supporting_node_type=[],
                            status="provisional",
                        )
                        result["data_crawled"] = data_crawled

                for pe in potential_entities:
                    pe_label = pe.get("label", "")
                    if pe_label and not any(e.get("text") == pe_label for e in primary_entities):
                        primary_entities.append({"label": pe.get("type", "").capitalize(), "text": pe_label})

            if question_type == "WHAT":
                logger.info("[MIXED/Override] question_type=WHAT, using FunctionFlowExecutor")
                executor = FunctionFlowExecutor()
                input_data = dict(override_state.get("input_data", override_state))

                # No continue-listing for MIXED mode (each sub-query is fresh)
                input_data["start_index"] = 0

                if query_plan.endswith(".yaml"):
                    flow_file_path = f"{self.graph_query_builder.config_base_path}/{query_plan}"
                else:
                    flow_file_path = f"{self.graph_query_builder.config_base_path}/{query_plan}.yaml"

                logger.info("[MIXED/Override] executing flow: %s", flow_file_path)
                final_state = executor.execute_flow(flow_file_path, input_data)
                final_state = sanitize_neo4j_types(final_state)

                result["query_results"] = self._normalize_what_query_results(
                    query_plan=query_plan,
                    input_data=input_data,
                    final_state=final_state,
                )

                intent = input_data.get("intent", "")
                current_year = datetime.now().year
                reasoning_time = input_data.get("time", {})
                reasoning_effective_from = input_data.get("time", {}).get("from_year", str(current_year))
                reasoning_effective_to = input_data.get("time", {}).get("to_year", str(current_year))
                if intent in ["attributes", "definition"]:
                    entity_attributes = final_state.get("entity_attributes", {})
                    fallback_attrs = final_state.get("fallback_major_attributes_to_academic_program", {})
                    enrich_input = None
                    if entity_attributes:
                        enrich_input = prepare_reasoning_input(
                            attribute_matches=entity_attributes,
                            effective_from=reasoning_effective_from,
                            effective_to=reasoning_effective_to,
                            time=reasoning_time,
                        )
                    elif fallback_attrs:
                        enrich_input = prepare_reasoning_input(
                            attribute_matches=fallback_attrs,
                            effective_from=reasoning_effective_from,
                            effective_to=reasoning_effective_to,
                            time=reasoning_time,
                        )
                    if enrich_input:
                        mock_state = {}
                        mock_ctx = _build_override_tool_context(mock_state)
                        process_reasoning(
                            tool_context=mock_ctx,
                            input_data=cast(NodeReasoningInput, enrich_input),
                            enrich_top_k=5,
                            similarity_threshold=0.7,
                        )
                        result["data_lv2_data_enriched_nodes"] = mock_state.get("data_lv2_data_enriched_nodes")
                        result["data_lv2_siblings_relative"] = mock_state.get("data_lv2_siblings_relative")
                        result["data_lv2_siblings_similar"] = mock_state.get("data_lv2_siblings_similar")

                if intent == "relation":
                    relation_input_raw = final_state.get("subgraph", {}).get("direct_connections", {})
                    if relation_input_raw:
                        relation_data_prepare = prepare_relation_enrich_input(
                            graph_data=relation_input_raw,
                            effective_from=reasoning_effective_from,
                            effective_to=reasoning_effective_to,
                            time=reasoning_time,
                        )

                        mock_state = {}
                        mock_ctx = _build_override_tool_context(mock_state)
                        process_relation_reasoning(
                            tool_context=mock_ctx,
                            input_data=relation_data_prepare,
                            enrich_top_k=5,
                            similarity_threshold=0.7,
                        )
                        result["data_lv2_data_enriched_relation"] = mock_state.get("data_lv2_data_enriched_relation")
                        result["data_lv2_related_nodes"] = mock_state.get("data_lv2_related_nodes")

            else:
                # WHY / HOW path
                if query_plan == "how_admission":
                    entities_for_flow = context_entities or [{"label": "University", "text": "Trường Đại học Gia Định"}]
                elif not primary_entities and not context_entities:
                    entities_for_flow = [{"label": "University", "text": "Trường Đại học Gia Định"}]
                else:
                    entities_for_flow = primary_entities
                intent = IntentClassification(
                    flow_config=FlowConfig(
                        flow_file=query_plan,
                        entities_to_resolve=cast(list[ExtractedEntity], entities_for_flow),
                        subtopics=subtopics,
                        time_filter=time_filter,
                    )
                )
                flow_file = intent.flow_config.flow_file + ".yaml"
                flow_config = self.graph_query_builder.load_flow_config(flow_file)
                requires_entity_id = flow_config.get("requires_entity_id", True)
                entities_to_resolve = intent.flow_config.entities_to_resolve
                resolved_entities = self._resolve_entities(
                    entities_to_resolve, requires_entity_id, flow_config, time_condition=time_filter
                )
                graph_results = self._execute_graph_traversal(
                    resolved_entities or [],
                    flow_file,
                    time_condition=time_filter,
                )
                graph_results = sanitize_neo4j_types(graph_results)
                graph_results = remove_key_recursive(graph_results, "cypher_query")
                graph_results = self._remove_redundant_keys(graph_results)
                normalized_query_results = graph_results.get("all_flow_results")
                if query_plan.startswith("how_"):
                    normalized_query_results = self._normalize_how_query_results(
                        query_plan=query_plan,
                        all_flow_results=graph_results.get("all_flow_results") or {},
                        crawled_data=result.get("data_crawled"),
                    )
                elif query_plan.startswith("why_"):
                    normalized_query_results = self._normalize_why_query_results(
                        query_plan=query_plan,
                        all_flow_results=graph_results.get("all_flow_results") or {},
                        crawled_data=result.get("data_crawled"),
                    )
                result["query_results"] = normalized_query_results

        except Exception as e:
            logger.exception("[MIXED/Override] Error in process_question_with_override: %s", e)

        return result

    # =========================================================================
    # STEP 2: ENTITY RESOLUTION
    # =========================================================================
    def _resolve_entities(
        self,
        entities_to_resolve: list,
        requires_entity_id: bool,
        flow_config: dict,
        time_condition: dict | None = None,
        # debug_info: Optional[dict]
    ) -> list[ResolvedEntity] | None:
        try:
            if requires_entity_id:
                # ==========================================================
                #
                #   - Output: entity_id + entity_name + similarity_score
                #
                # ==========================================================
                resolved_entities = self.entity_resolver.resolve_entities(
                    entities=entities_to_resolve, score_threshold=0.65, time_condition=time_condition
                )

                # Special case: only for how_admission flow, remap AcademicProgram
                # to parent Specialization/Major so traversal can continue correctly.
                if flow_config.get("flow_name") == "how_admission" and resolved_entities:
                    remapped_entities: list[ResolvedEntity] = []
                    for resolved_entity in resolved_entities:
                        if resolved_entity.entity_type in {"AcademicProgram", "Specialization"}:
                            parent_entity = self._resolve_major_for_how_admission_scope(
                                entity=resolved_entity, time_condition=time_condition
                            )
                            remapped_entities.append(parent_entity or resolved_entity)
                        else:
                            remapped_entities.append(resolved_entity)
                    resolved_entities = remapped_entities

                # if debug_info is not None:
                # 	debug_info["resolved_entities"] = [
                # 		e.model_dump() for e in resolved_entities
                # 	] if resolved_entities else []

                return resolved_entities if resolved_entities else None

            # ==========================================================
            #
            #
            # ==========================================================
            resolved_entities = [
                ResolvedEntity(
                    entity_id="42ec39d1-e985-4ebf-b52c-3eac2117cb6a",  # ID mặc định cho GDU
                    entity_name="Trường Đại học Gia Định",
                    entity_type=flow_config.get("supported_entity_types", ["University"])[0],
                    similarity_score=1.0,  # Score = 1.0 vì đây là exact match
                )
            ]

            # if debug_info is not None:
            # 	debug_info["resolved_entities"] = [
            # 		{"note": "requires_entity_id=false, using default entity (GDU)"}
            # 	]

            return resolved_entities
        except Exception:
            traceback.print_exc()
            # return ResolvedEntity()

    # =========================================================================
    # STEP 3: GRAPH TRAVERSAL
    # =========================================================================

    def _execute_graph_traversal(
        self,
        resolved_entities: list[ResolvedEntity],
        flow_file: str,
        time_condition: dict | None = None,
        # debug_info: Optional[dict]
    ) -> dict:
        # logger.error(f"[QueryOrchestrator] execute_graph_traversal")
        is_multi_entity = len(resolved_entities) >= 2

        all_flow_results: dict[str, QueryFlowResult] = {}
        effective_from, effective_to = _normalize_temporal_params(time_condition)
        # Preserve existing open-interval behavior for other flows.
        # For how_admission, when only one year is provided, constrain to that exact year window.
        if flow_file == "how_admission.yaml" and effective_from and not effective_to:
            effective_to = effective_from
        extra_params = {
            "effective_from": self._to_iso_start(effective_from),
            "effective_to": self._to_iso_end(effective_to),
        }

        all_contexts: list[str] = []

        for entity in resolved_entities:
            # logger.error(f"[QueryOrchestrator] execute_graph_traversal: {entity}")
            flow_result = self.graph_query_builder.execute_flow(
                flow_file=flow_file, resolved_entity=entity, extra_params=extra_params
            )

            all_flow_results[entity.entity_name] = flow_result

            all_contexts.append(f"## {entity.entity_name}\n{flow_result.raw_context}")

        # ==========================================================
        #
        # Single entity: Flat structure
        #   { "main_entity": {...}, "list_*": [...] }
        #
        # Multi entity: Nested by entity_name
        #   { "entities": { "CNTT": {...}, "Marketing": {...} } }
        # ==========================================================
        if is_multi_entity:
            # Multi-entity: Wrap results trong MultiEntityQueryResult
            multi_result = MultiEntityQueryResult(
                query_mode="multi",
                results=all_flow_results,
                combined_context="\n\n---\n\n".join(all_contexts),  # Nối context bằng separator
            )
            # Build nested evidence_json
            evidence_json = self._build_multi_evidence_json(all_flow_results)
        else:
            flow_result = all_flow_results[resolved_entities[0].entity_name]
            evidence_json = self._build_evidence_json(flow_result)
            multi_result = None  # Không cần wrapper

        # Add debug info
        # if debug_info is not None:
        # 	debug_info["graph_data"] = self._build_graph_debug(
        # 		multi_result, all_flow_results, resolved_entities
        # 	)
        # 	debug_info["evidence_json"] = evidence_json

        return {
            "is_multi_entity": is_multi_entity,
            "all_flow_results": all_flow_results,
            "multi_result": multi_result,
            "evidence_json": evidence_json,
        }

    # =========================================================================
    # EVIDENCE JSON BUILDERS
    # =========================================================================

    def _build_evidence_json(self, flow_result: QueryFlowResult) -> dict:
        evidence = {}

        # ==========================================================
        #
        # ==========================================================
        key_mapping = {
            "main_entity": "main_entity",  # Entity chính được hỏi
            "specializations": "list_Specialization",  # Các chuyên ngành
            "academic_programs": "list_AcademicProgram",  # Chương trình đào tạo
            "learning_outcomes": "list_ProgramLearningOutcome",  # Chuẩn đầu ra
            "market_trends": "list_MarketTrend",  # Xu hướng thị trường
            "faculty_info": "list_Faculty",  # Thông tin khoa
            "partners": "list_CollaborativePartner",  # Đối tác
        }

        # ==========================================================
        # ==========================================================
        for step_name, step_result in flow_result.traversal_results.items():
            key = key_mapping.get(step_name, f"list_{step_name}")

            if step_name == "main_entity" and step_result.data:
                evidence[key] = self._serialize_data(step_result.data[0])
            else:
                evidence[key] = self._serialize_data(step_result.data)

        return evidence

    def _build_multi_evidence_json(self, all_flow_results: dict[str, QueryFlowResult]) -> dict:
        # Container cho multi-entity evidence
        evidence = {"entities": {}}

        for entity_name, flow_result in all_flow_results.items():
            entity_evidence = self._build_evidence_json(flow_result)
            evidence["entities"][entity_name] = entity_evidence

        return evidence

    # =========================================================================
    # DATA SERIALIZATION
    # =========================================================================

    def _serialize_data(self, data):
        if isinstance(data, list):
            return [self._serialize_data(item) for item in data]
        elif isinstance(data, dict):
            return {k: self._serialize_data(v) for k, v in data.items()}
        elif hasattr(data, "isoformat"):
            return data.isoformat()
        elif hasattr(data, "__dict__"):
            return str(data)
        else:
            return data


# =============================================================================
# SINGLETON
# =============================================================================

_orchestrator: QueryOrchestrator | None = None


def get_orchestrator() -> QueryOrchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = QueryOrchestrator()
    return _orchestrator
