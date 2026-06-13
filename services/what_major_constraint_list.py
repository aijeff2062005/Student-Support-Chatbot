from __future__ import annotations

from datetime import datetime
from typing import Any

from dbs.graph_search_helpers import sanitize_neo4j_types
from dbs.what_ops_major_spec_academic import (
    _clean_props,
    _filter_effective_from_year_records,
    _has_time_filter,
    _has_value,
    _params_for_time,
    _temporal_and,
    _unique_strings,
)
from services.path_registry import list_all_by_label, list_via_path_registry
from services.what_relation_count_list_constraint import (
    _EXPAND_TYPE_MAP,
    _constraint_list_result,
    merge_constraint_search_queries,
)
from tools.QA.services.neo4j_service import get_neo4j_service
from utils.logging_config import get_logger

logger = get_logger(__name__)

ACADEMIC_TARGET_LABELS = ("Major", "Specialization")
ACADEMIC_PROGRAM_LABEL = "AcademicProgram"


def _node_id(item: dict[str, Any] | None) -> str:
    if not isinstance(item, dict):
        return ""
    return str(item.get("node_id") or item.get("id") or "").strip()


def _node_type(item: dict[str, Any] | None) -> str:
    if not isinstance(item, dict):
        return ""
    return str(item.get("node_type") or item.get("type") or "").strip()


def _time_with_current_year_fallback(time: dict[str, Any] | None = None) -> dict[str, Any]:
    if _has_time_filter(time):
        return dict(time or {})
    return {"from_year": datetime.now().year, "to_year": None}


def _compact_attribute_map(
    attrs: dict[str, Any] | None,
    display_attr_keys: list[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(attrs, dict):
        return {}
    display_keys = _unique_strings(display_attr_keys)
    if display_keys:
        return sanitize_neo4j_types(
            {key: attrs[key] for key in display_keys if key in attrs and _has_value(attrs[key])}
        )
    return sanitize_neo4j_types({key: value for key, value in attrs.items() if _has_value(value)})


def _compact_program(program: dict[str, Any], display_attr_keys: list[str] | None = None) -> dict[str, Any]:
    compacted = {
        "name": program.get("name"),
        "type": program.get("type") or ACADEMIC_PROGRAM_LABEL,
        "attributes": _compact_attribute_map(program.get("attributes") or {}, display_attr_keys),
    }
    if program.get("parent_specialization"):
        compacted["parent_specialization"] = program.get("parent_specialization")
    return sanitize_neo4j_types({key: value for key, value in compacted.items() if _has_value(value)})


def _compact_candidate(
    candidate: dict[str, Any],
    matched_attributes: dict[str, Any],
) -> dict[str, Any]:
    compacted = {
        "name": candidate.get("name"),
        "labels": candidate.get("labels") or [],
        "matched_attributes": sanitize_neo4j_types(matched_attributes),
    }
    return sanitize_neo4j_types({key: value for key, value in compacted.items() if _has_value(value)})


def _item_from_record(record: dict[str, Any]) -> dict[str, Any]:
    label = record.get("label") or record.get("type")
    item = {
        "name": record.get("name") or "",
        "id": record.get("id") or "",
        "labels": [label] if label else [],
    }
    if record.get("parent_major_name"):
        item["_parent_major"] = record.get("parent_major_name")
        item["_parent_major_id"] = record.get("parent_major_id")
    return sanitize_neo4j_types({key: value for key, value in item.items() if _has_value(value)})


def _fetch_academic_nodes(node_ids: list[str], time: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    node_ids = _unique_strings(node_ids)
    if not node_ids:
        return []

    _, effective_from, effective_to = _params_for_time(time)
    where_temporal = _temporal_and("n", effective_from, effective_to)
    query = f"""
    UNWIND $node_ids AS node_id
    MATCH (n {{id: node_id}})
    WHERE (n:Major OR n:Specialization) {where_temporal}
    OPTIONAL MATCH (parent:Major)-[:INCLUDES]->(n)
    RETURN DISTINCT n.id AS id,
           n.name AS name,
           labels(n)[0] AS label,
           parent.name AS parent_major_name,
           parent.id AS parent_major_id
    """
    params: dict[str, Any] = {"node_ids": node_ids}

    result = get_neo4j_service().cypher_query(query, params)
    if result.get("status") != "success":
        logger.error("_fetch_academic_nodes failed: %s", result.get("message"))
        return []
    by_id = {record.get("id"): _item_from_record(record) for record in result.get("results", [])}
    return [by_id[node_id] for node_id in node_ids if node_id in by_id]


def _normalize_major_spec_targets(target_topics: list[str] | None = None) -> list[str]:
    topics = _unique_strings(target_topics) or ["Major"]
    expanded: list[str] = []
    for topic in topics:
        expanded.extend(_EXPAND_TYPE_MAP.get(topic, [topic]))
    selected = [topic for topic in _unique_strings(expanded) if topic in ACADEMIC_TARGET_LABELS]
    return list(dict.fromkeys(selected or list(ACADEMIC_TARGET_LABELS)))


def build_major_spec_candidate_pool(
    context_found_list: list[dict[str, Any]] | None = None,
    target_topics: list[str] | None = None,
    time: dict[str, Any] | None = None,
    display_limit: int = 5,
    start_index: int = 0,
) -> dict[str, Any]:
    """
    Build the candidate set for major/specialization attribute constraints.

    Major and Specialization are intentionally peers here. Unlike cutoff-score
    routing, Specialization is not normalized to its parent Major.
    """
    context_found_list = [item for item in context_found_list or [] if isinstance(item, dict)]
    target_topics = _normalize_major_spec_targets(target_topics)
    start_index = start_index or 0
    display_limit = display_limit or 5

    context_types = {_node_type(item) for item in context_found_list}
    exact_scope = bool(context_types & set(ACADEMIC_TARGET_LABELS))

    if exact_scope:
        node_ids = [_node_id(item) for item in context_found_list if _node_type(item) in ACADEMIC_TARGET_LABELS]
        items = _fetch_academic_nodes(node_ids, time=time)
        total = len(items)
        return {
            "items": items,
            "total": total,
            "source": "major_constraint_exact_scope",
            "display_limit": display_limit,
            "start_index": start_index,
            "list_last_index": min(start_index + display_limit, total),
            "intermediate_nodes": [],
            "exact_scope": True,
        }

    if not context_found_list:
        pool = list_all_by_label(
            target_topics=target_topics,
            display_limit=display_limit,
            start_index=start_index,
            time=time,
        )
        pool["source"] = "major_constraint_global_pool"
        pool["exact_scope"] = False
        return pool

    first_context = context_found_list[0]
    context_type = _node_type(first_context)
    context_ids = [_node_id(item) for item in context_found_list if _node_id(item)]

    if context_type == "University":
        pool = list_all_by_label(
            target_topics=target_topics,
            display_limit=display_limit,
            start_index=start_index,
            time=time,
        )
    else:
        pool = list_via_path_registry(
            source_node_ids=context_ids,
            source_node_type=context_type,
            target_topics=target_topics,
            display_limit=display_limit,
            start_index=start_index,
            time=time,
        )
    pool["source"] = "major_constraint_candidate_pool"
    pool["exact_scope"] = False
    return pool


def build_major_constraint_raw_queries(
    keywords: list[str] | None = None,
    keyword_attributes: list[str] | None = None,
    resolved_attribute_keys: list[str] | None = None,
) -> list[str]:
    """
    Build value/semantic search queries.

    When attribute keys were resolved, raw queries should focus on value/filter
    terms from `keywords`; otherwise use keyword_attributes too so purely
    semantic constraints still have a fallback path.
    """
    queries = merge_constraint_search_queries(
        primary_texts=keywords,
        keyword_attributes=None if resolved_attribute_keys else keyword_attributes,
    )
    return [] if queries == [""] else queries


def build_academic_program_search_scope(
    candidate_pool: dict[str, Any] | None = None,
    time: dict[str, Any] | None = None,
) -> dict[str, Any]:
    candidate_pool = candidate_pool or {}
    candidates = [item for item in candidate_pool.get("items", []) if isinstance(item, dict) and item.get("id")]
    candidate_ids = _unique_strings(item.get("id") for item in candidates)
    if not candidate_ids:
        return {
            "search_node_ids": [],
            "candidate_by_search_node_id": {},
            "academic_programs_by_candidate_id": {},
            "exact_scope": bool(candidate_pool.get("exact_scope")),
        }

    effective_time = _time_with_current_year_fallback(time)
    _, effective_from, effective_to = _params_for_time(effective_time)
    direct_program_condition = f"ap IS NOT NULL {_temporal_and('ap', effective_from, effective_to)}".strip()
    specialization_program_condition = (
        f"ap2 IS NOT NULL {_temporal_and('ap2', effective_from, effective_to)}".strip()
    )

    query = f"""
    UNWIND $candidate_ids AS candidate_id
    MATCH (c {{id: candidate_id}})
    WHERE c:Major OR c:Specialization
    OPTIONAL MATCH (c)-[r:INCLUDES]->(ap:AcademicProgram)
    WITH c, collect(DISTINCT CASE WHEN {direct_program_condition} THEN {{
        id: ap.id,
        name: ap.name,
        type: labels(ap)[0],
        properties: properties(ap),
        parent_specialization: null
    }} ELSE null END) AS direct_programs
    OPTIONAL MATCH (c)-[r1:INCLUDES]->(s:Specialization)-[r2:INCLUDES]->(ap2:AcademicProgram)
    RETURN c.id AS candidate_id,
           direct_programs,
           collect(DISTINCT CASE WHEN {specialization_program_condition} THEN {{
               id: ap2.id,
               name: ap2.name,
               type: labels(ap2)[0],
               properties: properties(ap2),
               parent_specialization: {{id: s.id, name: s.name, type: labels(s)[0]}}
           }} ELSE null END) AS specialization_programs
    """
    params: dict[str, Any] = {"candidate_ids": candidate_ids}

    result = get_neo4j_service().cypher_query(query, params)
    if result.get("status") != "success":
        logger.error("build_academic_program_search_scope failed: %s", result.get("message"))
        return {
            "search_node_ids": candidate_ids,
            "candidate_by_search_node_id": {candidate_id: candidate_id for candidate_id in candidate_ids},
            "academic_programs_by_candidate_id": {},
            "exact_scope": bool(candidate_pool.get("exact_scope")),
        }

    candidate_by_search_node_id = {candidate_id: candidate_id for candidate_id in candidate_ids}
    academic_programs_by_candidate_id: dict[str, list[dict[str, Any]]] = {}

    for record in result.get("results", []):
        candidate_id = record.get("candidate_id")
        if not candidate_id:
            continue
        programs: list[dict[str, Any]] = []
        seen_program_ids: set[str] = set()
        for raw_program in [*(record.get("direct_programs") or []), *(record.get("specialization_programs") or [])]:
            if not isinstance(raw_program, dict) or not raw_program.get("id"):
                continue
            program_id = str(raw_program["id"])
            if program_id in seen_program_ids:
                continue
            seen_program_ids.add(program_id)
            program = {
                "id": program_id,
                "name": raw_program.get("name"),
                "type": raw_program.get("type") or ACADEMIC_PROGRAM_LABEL,
                "attributes": _clean_props(raw_program.get("properties") or {}),
                "parent_specialization": raw_program.get("parent_specialization"),
            }
            programs.append(sanitize_neo4j_types({k: v for k, v in program.items() if _has_value(v)}))
        programs = _filter_effective_from_year_records(programs, effective_time, properties_key="attributes")
        for program in programs:
            candidate_by_search_node_id[str(program["id"])] = candidate_id
        academic_programs_by_candidate_id[candidate_id] = programs

    search_node_ids = list(dict.fromkeys([*candidate_ids, *candidate_by_search_node_id.keys()]))
    return {
        "search_node_ids": search_node_ids,
        "candidate_by_search_node_id": candidate_by_search_node_id,
        "academic_programs_by_candidate_id": academic_programs_by_candidate_id,
        "exact_scope": bool(candidate_pool.get("exact_scope")),
    }


def _merge_attr_value(existing: Any, new_value: Any) -> Any:
    if existing is None:
        return new_value
    if existing == new_value:
        return existing
    values = existing if isinstance(existing, list) else [existing]
    if new_value not in values:
        values.append(new_value)
    return values


def build_major_constraint_list_results(
    candidate_pool: dict[str, Any] | None = None,
    search_scope: dict[str, Any] | None = None,
    exact_attr_matches: list[dict[str, Any]] | None = None,
    raw_attr_matches: list[dict[str, Any]] | None = None,
    keywords: list[str] | None = None,
    keyword_attributes: list[str] | None = None,
    resolved_attribute_keys: list[str] | None = None,
) -> dict[str, Any]:
    candidate_pool = candidate_pool or {}
    search_scope = search_scope or {}
    candidates = {
        str(item["id"]): item
        for item in candidate_pool.get("items", [])
        if isinstance(item, dict) and _has_value(item.get("id"))
    }
    candidate_by_search_node_id = search_scope.get("candidate_by_search_node_id") or {}
    programs_by_candidate = search_scope.get("academic_programs_by_candidate_id") or {}

    raw_attr_matches = raw_attr_matches or []
    exact_attr_matches = exact_attr_matches or []
    resolved_attribute_keys = _unique_strings(resolved_attribute_keys)
    # Do not hard-filter numeric/semantic conditions here; return compact evidence
    # from the scoped pool so the response LLM can make the final judgment.
    if resolved_attribute_keys and exact_attr_matches:
        filter_matches = exact_attr_matches
        enrich_matches = exact_attr_matches
    else:
        filter_matches = raw_attr_matches or exact_attr_matches
        enrich_matches = filter_matches

    allowed_candidate_ids: set[str] = set()
    matched_program_ids_by_candidate: dict[str, set[str]] = {}
    for match in filter_matches:
        node_id = str(match.get("node_id") or "")
        candidate_id = candidate_by_search_node_id.get(node_id)
        if not candidate_id:
            continue
        allowed_candidate_ids.add(candidate_id)
        if node_id != candidate_id:
            matched_program_ids_by_candidate.setdefault(candidate_id, set()).add(node_id)

    per_candidate: dict[str, dict[str, Any]] = {}
    for match in enrich_matches:
        node_id = str(match.get("node_id") or "")
        candidate_id = candidate_by_search_node_id.get(node_id)
        if not candidate_id or candidate_id not in allowed_candidate_ids:
            continue
        attr_name = match.get("attribute_name")
        if not attr_name:
            continue
        bucket = per_candidate.setdefault(
            candidate_id,
            {
                "matched_attributes": {},
            },
        )
        bucket["matched_attributes"][attr_name] = _merge_attr_value(
            bucket["matched_attributes"].get(attr_name),
            match.get("attribute_value"),
        )
        if node_id != candidate_id:
            matched_program_ids_by_candidate.setdefault(candidate_id, set()).add(node_id)

    rows: list[dict[str, Any]] = []
    exact_scope = bool(search_scope.get("exact_scope"))
    for candidate_id, match_data in per_candidate.items():
        candidate = dict(candidates.get(candidate_id) or {})
        if not candidate:
            continue
        programs = programs_by_candidate.get(candidate_id) or []
        matched_program_ids = matched_program_ids_by_candidate.get(candidate_id) or set()
        if exact_scope:
            selected_programs = programs
        elif matched_program_ids:
            selected_programs = [program for program in programs if program.get("id") in matched_program_ids]
        else:
            selected_programs = []

        matched_attributes = _compact_attribute_map(match_data.get("matched_attributes") or {}, resolved_attribute_keys)
        if not matched_attributes:
            continue

        row = _compact_candidate(candidate, matched_attributes)
        program_attr_keys = resolved_attribute_keys or list(matched_attributes.keys())
        compact_programs = [_compact_program(program, program_attr_keys) for program in selected_programs]
        compact_programs = [program for program in compact_programs if program]
        if compact_programs:
            row["academic_programs"] = compact_programs
        rows.append(row)

    rows.sort(key=lambda item: str(item.get("name") or ""))
    total = len(rows)
    return sanitize_neo4j_types(
        _constraint_list_result(
            rows,
            "major_attribute_constraint",
            display_limit=total,
            start_index=0,
            candidate_pool_total=candidate_pool.get("total", 0),
            matched_keywords=_unique_strings([*(keywords or []), *(keyword_attributes or [])]),
            exact_scope=exact_scope,
            selection_mode="llm_condition_review",
            resolved_attribute_keys=resolved_attribute_keys,
        )
    )
