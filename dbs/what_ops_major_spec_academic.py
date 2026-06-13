from __future__ import annotations

import asyncio
import unicodedata
from typing import Any

from dbs.graph_search_helpers import _normalize_temporal_params, sanitize_neo4j_types
from dbs.milvus_helper import search_attribute_definitions, search_entity_by_name, semantic_resolve_attributes
from schemas.query_plan_config import EntityType
from tools.QA.services.neo4j_service import build_temporal_where_clause, get_neo4j_service
from utils.logging_config import get_logger

logger = get_logger(__name__)


_ACADEMIC_SCOPE_LABELS = (
    EntityType.MAJOR.value,
    EntityType.SPECIALIZATION.value,
    EntityType.ACADEMIC_PROGRAM.value,
)
_ACADEMIC_SCOPE_LABEL_SET = set(_ACADEMIC_SCOPE_LABELS)
_INCLUDES_RELATION = "INCLUDES"
_IDENTITY_ATTRIBUTE_KEYS = ("name",)
_INTERNAL_PROP_KEYS = {
    "code",
    "created_at",
    "embedding",
    "id",
    "major_code",
    "node_type",
    "source_documents",
    "updated_at",
    "version",
    "visibility",
}
_SCOPE_NOT_FOUND_ANSWER = "Chưa xác định được ngành phù hợp với câu hỏi này."
_TEMPORAL_EMPTY_ANSWER = (
    "Chưa tìm thấy thông tin ngành, chuyên ngành hoặc chương trình đào tạo phù hợp với mốc thời gian được hỏi."
)
_NO_RELATED_ACADEMIC_ANSWER = "Hiện chưa có thông tin chương trình đào tạo hoặc chuyên ngành liên quan."
_PROGRAM_NAME_PREFIXES = (
    "[CHƯƠNG TRÌNH ĐÀO TẠO]",
    "[Chương trình đào tạo]",
    "Chương trình đào tạo:",
)
_STATE_MAJOR_NAME_KEYS = (
    "entity_name",
    "major_name",
    "specialization_name",
    "name",
    "title",
    "label",
    "text",
)
_STATE_MAJOR_TYPE_VALUES = {
    "academicprogram",
    "academic program",
    "major",
    "specialization",
    "nganh",
    "chuyen nganh",
}
_STATE_MAJOR_AUTO_RESOLVE_LIMIT = 2


def _has_value(value: Any) -> bool:
    return value not in (None, "", [], {})


def _has_time_value(value: Any) -> bool:
    return value not in (None, "", 0, "0")


def _unique_strings(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    return [text for text in dict.fromkeys(str(value).strip() for value in values or []) if text]


def _normalize_lookup_text(value: Any) -> str:
    text = unicodedata.normalize("NFD", str(value or "").lower().replace("_", " "))
    text = "".join(char for char in text if unicodedata.category(char) != "Mn")
    return " ".join(text.split())


def _is_public_attribute_key(key: Any) -> bool:
    key = str(key or "").strip()
    return bool(key) and key not in _INTERNAL_PROP_KEYS and not key.startswith("embedding")


def _iter_mappings(items: Any) -> list[dict[str, Any]]:
    if isinstance(items, dict):
        return [items]
    return [item for item in items or [] if isinstance(item, dict)]


def _looks_like_state_major_name(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    normalized = _normalize_lookup_text(text)
    if not normalized or normalized in _STATE_MAJOR_TYPE_VALUES or normalized.isdigit():
        return False
    return any(char.isalpha() for char in text)


def _iter_state_major_names(raw_items: Any) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()

    def add_name(value: Any) -> None:
        if not _looks_like_state_major_name(value):
            return
        name = str(value).strip()
        marker = _normalize_lookup_text(name)
        if marker in seen:
            return
        seen.add(marker)
        names.append(name)

    items = raw_items if isinstance(raw_items, list) else [raw_items] if raw_items else []
    for item in items:
        if isinstance(item, dict):
            keyed_values = [item.get(key) for key in _STATE_MAJOR_NAME_KEYS if item.get(key)]
            values = keyed_values or list(item.values())
        elif isinstance(item, list):
            for nested_name in _iter_state_major_names(item):
                add_name(nested_name)
            continue
        else:
            values = [item]

        for value in values:
            add_name(value)

    return names


def _select_state_major_names(
    interested_majors: list[dict[str, str]] | None = None,
    potential_majors: list[dict[str, str]] | None = None,
) -> tuple[list[str], str | None]:
    interested_names = _iter_state_major_names(interested_majors)
    if interested_names:
        return interested_names, "interested_majors"

    potential_names = _iter_state_major_names(potential_majors)
    if potential_names:
        return potential_names, "potential_majors"

    return [], None


def _requested_attribute_phrase(keyword_attributes: list[str] | None = None) -> str:
    keywords = _unique_strings(keyword_attributes)
    for keyword in keywords:
        text = str(keyword).strip()
        if text and "_" not in text:
            return text
    if keywords:
        return str(keywords[0]).replace("_", " ").strip()
    return "thông tin này"


def _state_major_clarification_answer(
    keyword_attributes: list[str] | None = None,
    candidate_names: list[str] | None = None,
) -> str:
    attribute_phrase = _requested_attribute_phrase(keyword_attributes)
    names = _unique_strings(candidate_names)
    if not names:
        return f"Bạn muốn hỏi {attribute_phrase} của ngành nào?"

    visible_names = names[:5]
    suffix = f" và {len(names) - len(visible_names)} ngành khác" if len(names) > len(visible_names) else ""
    return f"Bạn muốn hỏi {attribute_phrase} của ngành nào trong các ngành: {'; '.join(visible_names)}{suffix}?"


def _state_major_disambiguation_scope(
    keyword_attributes: list[str] | None = None,
    attribute_keys: list[str] | None = None,
    candidate_names: list[str] | None = None,
    source: str | None = None,
    route: str = "state_major_scope_missing",
) -> dict[str, Any]:
    names = _unique_strings(candidate_names)
    return sanitize_neo4j_types(
        {
            "status": "needs_disambiguation",
            "route": route,
            "state_major_source": source,
            "candidate_major_names": names,
            "summary": {
                "requested_attribute_keywords": keyword_attributes or [],
                "resolved_attribute_keys": _unique_strings(attribute_keys),
                "state_major_count": len(names),
            },
            "formatted_answer": _state_major_clarification_answer(keyword_attributes, names),
        }
    )


def _multi_state_major_scope(
    major_scopes: list[dict[str, Any]],
    major_names: list[str],
    source: str | None = None,
) -> dict[str, Any]:
    return sanitize_neo4j_types(
        {
            "status": "multi_major_scope",
            "route": "state_multi_major_scope",
            "state_major_source": source,
            "major_scopes": major_scopes,
            "summary": {
                "state_major_count": len(_unique_strings(major_names)),
                "resolved_major_count": len(major_scopes),
            },
        }
    )


def _first_mapping(items: list[Any] | None) -> dict[str, Any] | None:
    mappings = _iter_mappings(items)
    return mappings[0] if mappings else None


def _found_node_id(item: dict[str, Any] | None) -> str | None:
    if not isinstance(item, dict):
        return None
    node_id = str(item.get("node_id") or item.get("id") or "").strip()
    return node_id or None


def _found_entity_ref(item: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    entity = {
        "id": _found_node_id(item),
        "name": item.get("node_name") or item.get("name"),
        "type": item.get("node_type") or item.get("type"),
        "score": item.get("score"),
        "description": item.get("description"),
    }
    return sanitize_neo4j_types({key: value for key, value in entity.items() if _has_value(value)})


def _clean_props(props: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(props, dict):
        return {}
    return sanitize_neo4j_types({k: v for k, v in props.items() if _is_public_attribute_key(k) and _has_value(v)})


def _node_ref(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    props = _clean_props(row.get("properties") or {})
    node = {
        "id": row.get("id") or props.get("id"),
        "name": row.get("name") or props.get("name"),
        "type": row.get("type") or row.get("label") or props.get("node_type"),
        "description": row.get("description") or props.get("description"),
        "attributes": props,
    }
    return sanitize_neo4j_types({k: v for k, v in node.items() if _has_value(v)})


def _compact_attributes(props: dict[str, Any], attribute_keys: list[str] | None = None) -> dict[str, Any]:
    props = _clean_props(props)
    keys = _unique_strings(attribute_keys)
    if not keys:
        return props

    display_keys = _unique_strings([*_IDENTITY_ATTRIBUTE_KEYS, *keys])
    return sanitize_neo4j_types({key: props[key] for key in display_keys if key in props and _has_value(props[key])})


def _temporal_where(alias: str, effective_from: str | None, effective_to: str | None) -> str:
    clause = build_temporal_where_clause(alias, effective_from, effective_to)
    return f"WHERE {clause}" if clause else ""


def _temporal_and(alias: str, effective_from: str | None, effective_to: str | None) -> str:
    clause = build_temporal_where_clause(alias, effective_from, effective_to)
    return f"AND {clause}" if clause else ""


def _params_for_time(time: dict[str, Any] | None) -> tuple[dict[str, Any], str | None, str | None]:
    effective_from, effective_to = _normalize_temporal_params(time)
    params: dict[str, Any] = {}
    if effective_from:
        params["effective_from"] = effective_from
    if effective_to:
        params["effective_to"] = effective_to
    return params, effective_from, effective_to


def _neo4j_single(query: str, params: dict[str, Any]) -> dict[str, Any] | None:
    records = _neo4j_records(query, params)
    return records[0] if records else None


def _neo4j_records(query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    result = get_neo4j_service().cypher_query(query, params)
    if result.get("status") != "success":
        logger.error("Neo4j query failed: %s", result.get("message", "unknown error"))
        return []
    return [record for record in result.get("results", []) if isinstance(record, dict)]


def _run_async(coro: Any) -> Any:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    try:
        import nest_asyncio

        nest_asyncio.apply()
        return loop.run_until_complete(coro)
    except Exception as exc:
        logger.warning("async helper execution failed: %s", exc)
        return []


def _academic_attribute_keys() -> tuple[str, ...]:
    query = """
    MATCH (n)
    WHERE any(label IN labels(n) WHERE label IN $labels)
    WITH keys(n) AS node_keys
    UNWIND node_keys AS key
    RETURN collect(DISTINCT key) AS keys
    """
    records = _neo4j_records(query, {"labels": list(_ACADEMIC_SCOPE_LABELS)})
    keys = _unique_strings(records[0].get("keys") if records else [])
    return tuple(key for key in keys if _is_public_attribute_key(key))


def _has_time_filter(time: dict[str, Any] | None) -> bool:
    if not isinstance(time, dict):
        return False
    return any(_has_time_value(time.get(key)) for key in ("year", "from_year", "to_year", "raw"))


def _year_from_value(value: Any) -> int | None:
    if not value:
        return None
    try:
        return int(str(value)[:4])
    except (TypeError, ValueError):
        return None


def _effective_from_year(record: dict[str, Any], properties_key: str = "properties") -> int | None:
    props = record.get(properties_key) or {}
    if not isinstance(props, dict):
        return None
    return _year_from_value(props.get("effective_from"))


def _requested_effective_from_year_window(time: dict[str, Any] | None) -> tuple[int | None, int | None]:
    if not isinstance(time, dict):
        return None, None

    normalized_from, _ = _normalize_temporal_params(time)
    start_year = _year_from_value(time.get("from_year") or time.get("year") or normalized_from)
    if start_year is None:
        return None, None

    end_year = _year_from_value(time.get("to_year")) or start_year
    return (end_year, start_year) if end_year < start_year else (start_year, end_year)


def _filter_effective_from_year_records(
    records: list[dict[str, Any]],
    time: dict[str, Any] | None,
    properties_key: str = "properties",
) -> list[dict[str, Any]]:
    """Keep only records whose effective_from year matches the requested year/window."""
    if not _has_time_filter(time) or not records:
        return records

    start_year, end_year = _requested_effective_from_year_window(time)
    if start_year is None or end_year is None:
        return records

    return [
        record
        for record in records
        if (effective_year := _effective_from_year(record, properties_key)) is not None
        and start_year <= effective_year <= end_year
    ]


def _temporal_and_clauses(
    aliases: tuple[str, ...],
    effective_from: str | None,
    effective_to: str | None,
) -> dict[str, str]:
    return {alias: _temporal_and(alias, effective_from, effective_to) for alias in aliases}


def _prefixed_node_ref(record: dict[str, Any], prefix: str) -> dict[str, Any] | None:
    return _node_ref(
        {
            "id": record.get(f"{prefix}_id"),
            "name": record.get(f"{prefix}_name"),
            "type": record.get(f"{prefix}_type"),
            "properties": record.get(f"{prefix}_properties"),
        }
    )


def _academic_program_row(
    record: dict[str, Any],
    attribute_keys: list[str] | None,
) -> dict[str, Any] | None:
    node = _node_ref(record)
    if not node:
        return None
    return {
        **node,
        "attributes": _compact_attributes(record.get("properties") or {}, attribute_keys),
        "relationship_type": record.get("relationship_type"),
        "relationship_properties": _clean_props(record.get("relationship_properties") or {}),
    }


def _academic_program_rows(
    records: list[dict[str, Any]],
    attribute_keys: list[str] | None,
    time: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    rows = []
    for record in _filter_effective_from_year_records(records, time):
        row = _academic_program_row(record, attribute_keys)
        if row:
            rows.append(row)
    return sanitize_neo4j_types(rows)


def _direct_relationship_groups(
    target_ids: list[str],
    candidate_label: str,
) -> dict[str, Any]:
    target_ids = _unique_strings(target_ids)
    if not target_ids:
        return {}

    return get_neo4j_service()._check_direct_connection(
        target_node_ids=target_ids,
        candidate_labels=[candidate_label],
    )


def _relationship_candidate_record(candidate: dict[str, Any]) -> dict[str, Any]:
    props = candidate.get("candidate_properties") or {}
    return {
        "id": candidate.get("candidate_id") or props.get("id"),
        "name": candidate.get("candidate_name") or props.get("name"),
        "type": props.get("node_type"),
        "properties": props,
        "relationship_type": candidate.get("relationship_type"),
        "relationship_properties": candidate.get("relationship_properties") or {},
    }


def _included_candidate_records(groups: dict[str, Any], target_id: str) -> list[dict[str, Any]]:
    group = groups.get(target_id) if isinstance(groups, dict) else None
    return [
        _relationship_candidate_record(candidate)
        for candidate in _iter_mappings((group or {}).get("candidates") if isinstance(group, dict) else [])
        if candidate.get("relationship_type") == _INCLUDES_RELATION
    ]


def _resolve_known_attribute_keys(
    keywords: list[str],
    similarity_threshold: float,
) -> list[str]:
    known_keys = _academic_attribute_keys()
    normalized_to_key = {_normalize_lookup_text(key): key for key in known_keys}
    resolved: list[str] = []

    for keyword in keywords:
        normalized_keyword = _normalize_lookup_text(keyword)
        if not normalized_keyword:
            continue

        if normalized_keyword in normalized_to_key:
            resolved.append(normalized_to_key[normalized_keyword])
            continue

        underscored_keyword = normalized_keyword.replace(" ", "_")
        if underscored_keyword in known_keys:
            resolved.append(underscored_keyword)

    if exact_keys := _unique_strings(resolved):
        return exact_keys

    try:
        matches = _run_async(
            semantic_resolve_attributes(
                requested_attributes=keywords,
                available_attribute_keys=known_keys,
                similarity_threshold=max(similarity_threshold, 0.65),
                top_k=1,
            )
        )
    except Exception as exc:
        logger.warning("semantic attribute lookup failed: %s", exc)
        return []

    return _unique_strings(
        match.get("attribute_key") for match in _iter_mappings(matches) if match.get("attribute_key") in known_keys
    )


def _resolve_attribute_definition_keys(
    keywords: list[str],
    top_k: int,
    similarity_threshold: float,
) -> list[str]:
    try:
        matches = search_attribute_definitions(
            keywords=keywords,
            label=None,
            similarity_threshold=similarity_threshold,
            top_k=max(top_k * len(_ACADEMIC_SCOPE_LABELS), top_k),
        )
    except Exception as exc:
        logger.warning("attribute definition lookup failed: %s", exc)
        return []

    return _unique_strings(
        match.get("attribute_name")
        for match in matches or []
        if match.get("label") in _ACADEMIC_SCOPE_LABEL_SET and match.get("attribute_name")
    )


def resolve_major_spec_academic_attribute_keys(
    keyword_attributes: list[str] | None = None,
    top_k: int = 5,
    similarity_threshold: float = 0.6,
) -> list[str]:
    """
    Resolve user attribute words against Major/Specialization/AcademicProgram.

    This is intentionally broad because this operation always pivots back to
    Major, then reads down into AcademicProgram or Specialization branches.
    """
    keywords = _unique_strings(keyword_attributes)
    if not keywords:
        return []

    resolved = _resolve_known_attribute_keys(keywords, similarity_threshold)
    if resolved:
        return resolved

    return _resolve_attribute_definition_keys(keywords, top_k, similarity_threshold)


def resolve_major_scope_from_entity(
    found_list: list[dict[str, Any]] | None,
    time: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Normalize any Major/Specialization/AcademicProgram input back to its Major.

    Business rule:
    - Major input: use itself.
    - Specialization input: use parent Major.
    - AcademicProgram input: use parent Major, either direct Major->AP or
      Major->Specialization->AP.
    """
    found = _first_mapping(found_list)
    input_id = _found_node_id(found)
    if not input_id:
        return {}

    params, effective_from, effective_to = _params_for_time(time)
    params["input_id"] = input_id
    temporal = _temporal_and_clauses(("r", "r1", "r2", "m", "s"), effective_from, effective_to)
    node_input = _temporal_where("input", effective_from, effective_to)

    query = f"""
    MATCH (input {{id: $input_id}})
    {node_input}
    CALL (input) {{
      WITH input WHERE input:Major
      RETURN input AS major, null AS specialization, 'input_major' AS route, 0 AS route_priority

      UNION
      WITH input WHERE input:Specialization
      MATCH (m:Major)-[r:INCLUDES]->(input)
      WHERE true {temporal["r"]} {temporal["m"]}
      RETURN m AS major, input AS specialization, 'specialization_parent_major' AS route, 1 AS route_priority

      UNION
      WITH input WHERE input:AcademicProgram
      MATCH (m:Major)-[r:INCLUDES]->(input)
      WHERE true {temporal["r"]} {temporal["m"]}
      RETURN m AS major, null AS specialization, 'academic_direct_major' AS route, 1 AS route_priority

      UNION
      WITH input WHERE input:AcademicProgram
      MATCH (m:Major)-[r1:INCLUDES]->(s:Specialization)-[r2:INCLUDES]->(input)
      WHERE true {temporal["r1"]} {temporal["r2"]} {temporal["m"]} {temporal["s"]}
      RETURN m AS major, s AS specialization, 'academic_via_specialization_parent_major' AS route, 2 AS route_priority
    }}
    RETURN
      input.id AS input_id,
      input.name AS input_name,
      labels(input)[0] AS input_type,
      properties(input) AS input_properties,
      major.id AS major_id,
      major.name AS major_name,
      labels(major)[0] AS major_type,
      properties(major) AS major_properties,
      specialization.id AS specialization_id,
      specialization.name AS specialization_name,
      labels(specialization)[0] AS specialization_type,
      properties(specialization) AS specialization_properties,
      route,
      route_priority
    ORDER BY route_priority ASC, major_name ASC
    LIMIT 1
    """

    record = _neo4j_single(query, params)

    if not record:
        return {
            "input_entity": _found_entity_ref(found),
            "major": None,
            "route": "major_scope_not_found",
        }

    result = {
        "input_entity": _prefixed_node_ref(record, "input"),
        "major": _prefixed_node_ref(record, "major"),
        "parent_specialization": _prefixed_node_ref(record, "specialization"),
        "route": record["route"],
    }
    return sanitize_neo4j_types({k: v for k, v in result.items() if v})


def resolve_major_scope_from_state_majors(
    interested_majors: list[dict[str, str]] | None = None,
    potential_majors: list[dict[str, str]] | None = None,
    keyword_attributes: list[str] | None = None,
    attribute_keys: list[str] | None = None,
    time: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Resolve omitted academic scope from state-level interested/potential majors.

    The parser only needs to route these questions to the Major academic flow.
    This step owns the state check: one or two state majors are resolved to
    graph Major scopes; zero or more than two state majors produce a clarification.
    """
    major_names, source = _select_state_major_names(
        interested_majors=interested_majors,
        potential_majors=potential_majors,
    )
    if not major_names or len(major_names) > _STATE_MAJOR_AUTO_RESOLVE_LIMIT:
        route = "state_major_scope_missing" if not major_names else "state_major_scope_ambiguous"
        return _state_major_disambiguation_scope(
            keyword_attributes=keyword_attributes,
            attribute_keys=attribute_keys,
            candidate_names=major_names,
            source=source,
            route=route,
        )

    try:
        found_list = search_entity_by_name(
            entity_name=major_names,
            entity_type=[EntityType.MAJOR.value, EntityType.SPECIALIZATION.value],
            top_k=1,
            threshold=0.55,
            time=time,
        )
    except Exception as exc:
        logger.error("resolve_major_scope_from_state_majors failed for %s: %s", major_names, exc)
        return _state_major_disambiguation_scope(
            keyword_attributes=keyword_attributes,
            attribute_keys=attribute_keys,
            candidate_names=major_names,
            source=source,
            route="state_major_scope_resolution_failed",
        )

    valid_found = [
        item
        for item in _iter_mappings(found_list)
        if (item.get("node_type") or item.get("type")) in {EntityType.MAJOR.value, EntityType.SPECIALIZATION.value}
    ]
    if len(valid_found) < len(major_names):
        return _state_major_disambiguation_scope(
            keyword_attributes=keyword_attributes,
            attribute_keys=attribute_keys,
            candidate_names=major_names,
            source=source,
            route="state_major_scope_resolution_failed",
        )

    major_scopes: list[dict[str, Any]] = []
    seen_major_ids: set[str] = set()
    for major_name, found in zip(major_names, valid_found, strict=False):
        major_scope = resolve_major_scope_from_entity([found], time=time)
        if not isinstance(major_scope, dict) or not isinstance(major_scope.get("major"), dict):
            return _state_major_disambiguation_scope(
                keyword_attributes=keyword_attributes,
                attribute_keys=attribute_keys,
                candidate_names=major_names,
                source=source,
                route="state_major_scope_resolution_failed",
            )

        major_scope["state_major_source"] = source
        major_scope["state_major_name"] = major_name
        major_scope["route"] = f"state_{major_scope.get('route') or 'major_scope'}"
        major_id = str((major_scope.get("major") or {}).get("id") or "").strip()
        if major_id and major_id in seen_major_ids:
            continue
        if major_id:
            seen_major_ids.add(major_id)
        major_scopes.append(sanitize_neo4j_types(major_scope))

    if len(major_scopes) == 1:
        return sanitize_neo4j_types(major_scopes[0])
    return _multi_state_major_scope(major_scopes, major_names, source)


def _fetch_node(node_id: str, label: str, time: dict[str, Any] | None = None) -> dict[str, Any] | None:
    rows = get_neo4j_service().comparison_search_neo4j(
        node_ids=[node_id],
        node_types=[label],
        time=time,
    )
    if not rows:
        return None
    row = rows[0]
    return _node_ref(
        {
            "id": row.get("node_id"),
            "name": row.get("name"),
            "type": (row.get("labels") or [label])[0],
            "description": row.get("description"),
            "properties": row.get("properties") or {},
        }
    )


def _fetch_direct_academic_programs(
    major_id: str,
    attribute_keys: list[str] | None,
    time: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    groups = _direct_relationship_groups([major_id], EntityType.ACADEMIC_PROGRAM.value)
    return _academic_program_rows(_included_candidate_records(groups, major_id), attribute_keys, time)


def _fetch_specialization_branches(
    major_id: str,
    attribute_keys: list[str] | None,
    time: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    spec_groups = _direct_relationship_groups([major_id], EntityType.SPECIALIZATION.value)
    spec_records = _included_candidate_records(spec_groups, major_id)
    if not spec_records:
        return []

    spec_ids = [record["id"] for record in spec_records if record.get("id")]
    program_groups = _direct_relationship_groups(spec_ids, EntityType.ACADEMIC_PROGRAM.value)
    branches = []
    for record in sorted(spec_records, key=lambda item: str(item.get("name") or "")):
        spec = _node_ref(record)
        if not spec:
            continue

        programs = _academic_program_rows(
            _included_candidate_records(program_groups, record.get("id")),
            attribute_keys,
            time,
        )

        branches.append(
            {
                **spec,
                "attributes": _compact_attributes(record.get("properties") or {}, attribute_keys),
                "academic_programs": programs,
            }
        )

    return sanitize_neo4j_types(branches)


def _major_scope_not_found_bundle(major_scope: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "status": "major_scope_not_found",
        "input_entity": (major_scope or {}).get("input_entity") if isinstance(major_scope, dict) else None,
        "formatted_answer": (major_scope or {}).get("formatted_answer") or _SCOPE_NOT_FOUND_ANSWER,
    }


def _bundle_summary(
    keyword_attributes: list[str] | None,
    resolved_keys: list[str],
    time: dict[str, Any] | None,
    direct_programs: list[dict[str, Any]] | None = None,
    specializations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    direct_programs = direct_programs or []
    specializations = specializations or []
    return {
        "requested_attribute_keywords": keyword_attributes or [],
        "resolved_attribute_keys": resolved_keys,
        "time_filter": time or {},
        "has_direct_academic_programs": bool(direct_programs),
        "specialization_count": len(specializations),
        "academic_program_count": len(direct_programs)
        + sum(len(specialization.get("academic_programs") or []) for specialization in specializations),
    }


def _temporal_empty_bundle(
    major_scope: dict[str, Any],
    resolved_keys: list[str],
    keyword_attributes: list[str] | None,
    time: dict[str, Any] | None,
) -> dict[str, Any]:
    return sanitize_neo4j_types(
        {
            "status": "empty",
            "input_entity": major_scope.get("input_entity"),
            "major": major_scope.get("major"),
            "parent_specialization": major_scope.get("parent_specialization"),
            "summary": _bundle_summary(keyword_attributes, resolved_keys, time),
            "route": major_scope.get("route"),
            "formatted_answer": _TEMPORAL_EMPTY_ANSWER,
        }
    )


def _academic_branches_for_major(
    major_id: str,
    resolved_keys: list[str],
    time: dict[str, Any] | None,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    direct_programs = _fetch_direct_academic_programs(major_id, resolved_keys, time)
    if direct_programs:
        return "major_direct_academic_program", direct_programs, []

    return (
        "major_via_specialization_academic_program",
        [],
        _fetch_specialization_branches(major_id, resolved_keys, time),
    )


def collect_major_spec_academic_bundle(
    major_scope: dict[str, Any] | None,
    attribute_keys: list[str] | None = None,
    keyword_attributes: list[str] | None = None,
    time: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Build the business bundle rooted at Major.

    Always starts from Major. Direct Major->AcademicProgram wins. Only when
    direct AcademicProgram is absent do we expand Major->Specialization->AcademicProgram.
    """
    if isinstance(major_scope, dict) and major_scope.get("status") == "needs_disambiguation":
        return sanitize_neo4j_types(
            {
                "status": "needs_disambiguation",
                "route": major_scope.get("route"),
                "state_major_source": major_scope.get("state_major_source"),
                "candidate_major_names": major_scope.get("candidate_major_names") or [],
                "summary": {
                    **(major_scope.get("summary") or {}),
                    "resolved_attribute_keys": _unique_strings(attribute_keys),
                    "time_filter": time or {},
                },
                "formatted_answer": major_scope.get("formatted_answer")
                or _state_major_clarification_answer(keyword_attributes),
            }
        )

    if isinstance(major_scope, dict) and major_scope.get("status") == "multi_major_scope":
        major_bundles: list[dict[str, Any]] = []
        for scoped_major in _iter_mappings(major_scope.get("major_scopes")):
            bundle = collect_major_spec_academic_bundle(
                major_scope=scoped_major,
                attribute_keys=attribute_keys,
                keyword_attributes=keyword_attributes,
                time=time,
            )
            if isinstance(bundle, dict) and bundle.get("status") == "ok":
                major_bundles.append(bundle)

        if not major_bundles:
            return _major_scope_not_found_bundle(major_scope)

        return sanitize_neo4j_types(
            {
                "status": "ok",
                "route": major_scope.get("route") or "state_multi_major_scope",
                "state_major_source": major_scope.get("state_major_source"),
                "major_bundles": major_bundles,
                "summary": {
                    **(major_scope.get("summary") or {}),
                    "requested_attribute_keywords": keyword_attributes or [],
                    "resolved_attribute_keys": _unique_strings(attribute_keys),
                    "time_filter": time or {},
                },
            }
        )

    if not isinstance(major_scope, dict) or not isinstance(major_scope.get("major"), dict):
        return _major_scope_not_found_bundle(major_scope)

    major_id = major_scope["major"].get("id")
    if not major_id:
        return _major_scope_not_found_bundle(major_scope)

    resolved_keys = _unique_strings(attribute_keys)
    major_node = _fetch_node(major_id, EntityType.MAJOR.value, time=time)
    if not major_node and _has_time_filter(time):
        return _temporal_empty_bundle(major_scope, resolved_keys, keyword_attributes, time)

    major_node = major_node or major_scope["major"]
    major_attributes = _compact_attributes((major_node or {}).get("attributes") or {}, resolved_keys)
    source_branch, direct_programs, specializations = _academic_branches_for_major(major_id, resolved_keys, time)

    bundle = {
        "status": "ok" if direct_programs or specializations or major_node else "empty",
        "source_branch": source_branch,
        "input_entity": major_scope.get("input_entity"),
        "major": {
            **major_node,
            "attributes": major_attributes,
        },
        "parent_specialization": major_scope.get("parent_specialization"),
        "direct_academic_programs": direct_programs,
        "specializations": specializations,
        "summary": _bundle_summary(keyword_attributes, resolved_keys, time, direct_programs, specializations),
        "route": major_scope.get("route"),
    }
    return sanitize_neo4j_types(bundle)


def merge_major_compare_found_lists(
    primary_found_list: list[dict[str, Any]] | None = None,
    compare_found_list: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Merge resolved compare entities in the order the user mentioned them."""
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()

    for item in _iter_mappings([*(primary_found_list or []), *(compare_found_list or [])]):
        node_id = _found_node_id(item)
        if not node_id or node_id in seen:
            continue
        seen.add(node_id)
        merged.append(item)

    return merged


def _resolve_compare_attribute_keys(
    keyword_attributes: list[str] | None = None,
    default_attribute_keys: list[str] | None = None,
) -> list[str]:
    resolved_keys = resolve_major_spec_academic_attribute_keys(keyword_attributes=keyword_attributes)
    if resolved_keys:
        return resolved_keys
    return _unique_strings(default_attribute_keys)


def _compare_row_from_bundle(bundle: dict[str, Any], found: dict[str, Any]) -> dict[str, Any]:
    input_entity = bundle.get("input_entity") or {}
    major = bundle.get("major") or {}
    row_type = input_entity.get("type") or found.get("node_type") or major.get("type") or "Major"
    row_name = input_entity.get("name") or found.get("node_name") or found.get("name") or major.get("name")
    properties = {
        "major": major,
        "input_entity": input_entity,
        "parent_specialization": bundle.get("parent_specialization"),
        "direct_academic_programs": bundle.get("direct_academic_programs") or [],
        "specializations": bundle.get("specializations") or [],
        "summary": bundle.get("summary") or {},
        "source_branch": bundle.get("source_branch"),
        "route": bundle.get("route"),
    }

    return {
        "node_id": input_entity.get("id") or _found_node_id(found) or major.get("id"),
        "labels": [row_type],
        "name": row_name,
        "description": input_entity.get("description") or major.get("description") or found.get("description"),
        "properties": sanitize_neo4j_types(properties),
    }


def build_major_program_compare_results(
    found_list: list[dict[str, Any]] | None = None,
    attribute_keys: list[str] | None = None,
    keyword_attributes: list[str] | None = None,
    time: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Build compare rows from the same Major-rooted academic tree used by
    what_ops_major_spec_academic.
    """
    if not found_list:
        return []

    resolved_keys = _resolve_compare_attribute_keys(
        keyword_attributes=keyword_attributes,
        default_attribute_keys=attribute_keys,
    )
    rows: list[dict[str, Any]] = []

    for found in _iter_mappings(found_list):
        major_scope = resolve_major_scope_from_entity([found], time=time)
        bundle = collect_major_spec_academic_bundle(
            major_scope=major_scope,
            attribute_keys=resolved_keys,
            keyword_attributes=keyword_attributes,
            time=time,
        )
        if not isinstance(bundle, dict) or bundle.get("status") != "ok":
            continue

        rows.append(_compare_row_from_bundle(bundle, found))

    return sanitize_neo4j_types(rows)


def _display_name(name: Any, fallback: str) -> str:
    text = str(name or fallback).strip()
    for prefix in _PROGRAM_NAME_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
    return text or fallback


def _entity_title(entity: dict[str, Any], fallback: str) -> str:
    return _display_name(entity.get("name"), fallback)


def _join_titles(items: list[dict[str, Any]], fallback: str, limit: int = 6) -> str:
    titles = [_entity_title(item, fallback) for item in items[:limit]]
    suffix = f" và {len(items) - limit} mục khác" if len(items) > limit else ""
    return "; ".join(titles) + suffix


def _format_attribute_value(value: Any) -> str | None:
    if not _has_value(value):
        return None

    if isinstance(value, dict):
        if _has_value(value.get("value")):
            return _format_attribute_value(value.get("value"))

        parts = []
        for key, item_value in value.items():
            if not _is_public_attribute_key(key) or not _has_value(item_value):
                continue
            item_text = _format_attribute_value(item_value)
            if item_text:
                parts.append(f"{str(key).replace('_', ' ')}: {item_text}")
        return "; ".join(parts) if parts else None

    if isinstance(value, (list, tuple, set)):
        parts = []
        for item in value:
            item_text = _format_attribute_value(item)
            if item_text:
                parts.append(item_text)
        return "; ".join(_unique_strings(parts)) if parts else None

    text = str(value).strip()
    return text or None


def _requested_attribute_keys_from_bundle(bundle: dict[str, Any]) -> list[str]:
    summary = bundle.get("summary") if isinstance(bundle, dict) else {}
    return _unique_strings((summary or {}).get("resolved_attribute_keys"))


def _attribute_label_from_request(bundle: dict[str, Any], attribute_key: str) -> str:
    summary = bundle.get("summary") if isinstance(bundle, dict) else {}
    keywords = _unique_strings((summary or {}).get("requested_attribute_keywords"))
    if len(_requested_attribute_keys_from_bundle(bundle)) == 1:
        for keyword in keywords:
            if "_" not in keyword:
                return keyword
    return str(attribute_key or "").replace("_", " ").strip()


def _attribute_scope_groups(bundle: dict[str, Any]) -> list[list[tuple[str, dict[str, Any]]]]:
    groups: list[list[tuple[str, dict[str, Any]]]] = []

    direct_programs = [
        (_entity_title(program, "Chương trình đào tạo"), program.get("attributes") or {})
        for program in _iter_mappings(bundle.get("direct_academic_programs"))
    ]
    if direct_programs:
        groups.append(direct_programs)

    spec_programs: list[tuple[str, dict[str, Any]]] = []
    specializations: list[tuple[str, dict[str, Any]]] = []
    for specialization in _iter_mappings(bundle.get("specializations")):
        specialization_name = _entity_title(specialization, "Chuyên ngành")
        for program in _iter_mappings(specialization.get("academic_programs")):
            program_name = _entity_title(program, "Chương trình đào tạo")
            spec_programs.append((f"{program_name} ({specialization_name})", program.get("attributes") or {}))
        specializations.append((specialization_name, specialization.get("attributes") or {}))

    if spec_programs:
        groups.append(spec_programs)
    if specializations:
        groups.append(specializations)

    major = bundle.get("major") or {}
    if isinstance(major, dict):
        groups.append([(_entity_title(major, "Ngành"), major.get("attributes") or {})])

    return groups


def _attribute_values_for_key(bundle: dict[str, Any], attribute_key: str) -> list[tuple[str, str]]:
    for group in _attribute_scope_groups(bundle):
        values = []
        for scope_name, attributes in group:
            if not isinstance(attributes, dict) or attribute_key not in attributes:
                continue
            value_text = _format_attribute_value(attributes.get(attribute_key))
            if value_text:
                values.append((scope_name, value_text))
        if values:
            return values
    return []


def _attribute_value_summary(values: list[tuple[str, str]], limit: int = 5) -> str:
    unique_values = _unique_strings(value for _, value in values)
    if len(unique_values) == 1:
        return f"**{unique_values[0]}**"

    parts = []
    seen: set[tuple[str, str]] = set()
    for scope_name, value in values:
        marker = (scope_name, value)
        if marker in seen:
            continue
        seen.add(marker)
        parts.append(f"{scope_name}: **{value}**")
        if len(parts) >= limit:
            break
    if len(values) > limit:
        parts.append(f"{len(values) - limit} mục khác")
    return "; ".join(parts)


def _specialization_branch_summary(bundle: dict[str, Any]) -> str:
    specializations = _iter_mappings(bundle.get("specializations"))
    if not specializations:
        return ""

    specialization_names = _unique_strings(_entity_title(item, "Chuyên ngành") for item in specializations)
    visible_names = specialization_names[:5]
    suffix = f" và {len(specialization_names) - len(visible_names)} chuyên ngành khác" if len(
        specialization_names
    ) > len(visible_names) else ""
    return f"được tổ chức theo **{len(specializations)}** chuyên ngành như {'; '.join(visible_names)}{suffix}"


def _format_requested_attribute_answer(bundle: dict[str, Any]) -> str | None:
    major = bundle.get("major") or {}
    major_name = _display_name(major.get("name"), "ngành được hỏi")
    attribute_summaries = []
    for attribute_key in _requested_attribute_keys_from_bundle(bundle):
        values = _attribute_values_for_key(bundle, attribute_key)
        if values:
            attribute_summaries.append((attribute_key, _attribute_value_summary(values)))

    if not attribute_summaries:
        return None

    if len(attribute_summaries) == 1:
        attribute_key, value_summary = attribute_summaries[0]
        label = _attribute_label_from_request(bundle, attribute_key)
        if not bundle.get("direct_academic_programs") and bundle.get("specializations"):
            branch_summary = _specialization_branch_summary(bundle)
            if branch_summary:
                return f"**{major_name}** {branch_summary}; {label}: {value_summary}."
        return f"**{major_name}** - {label}: {value_summary}."

    lines = [f"**{major_name}** có các thông tin học thuật phù hợp:"]
    for attribute_key, value_summary in attribute_summaries:
        lines.append(f"- {_attribute_label_from_request(bundle, attribute_key)}: {value_summary}.")

    return "\n".join(lines)


def _format_multi_major_spec_academic_answer(bundle: dict[str, Any], original_query: str | None = None) -> str:
    major_bundles = [item for item in bundle.get("major_bundles") or [] if isinstance(item, dict)]
    if not major_bundles:
        return _SCOPE_NOT_FOUND_ANSWER

    target_phrase = "cả hai ngành" if len(major_bundles) == 2 else "các ngành này"
    lines = [f"Mình thấy bạn đang quan tâm **{len(major_bundles)} ngành**, nên trả thông tin cho {target_phrase}:"]
    for item in major_bundles:
        major = item.get("major") or {}
        major_name = _display_name(major.get("name"), "ngành được hỏi")
        answer = format_major_spec_academic_answer(item, original_query=original_query).replace("\n", " ").strip()
        if answer.startswith(f"**{major_name}**"):
            lines.append(f"- {answer}")
        else:
            lines.append(f"- **{major_name}**: {answer}")
    return "\n".join(lines)


def format_major_spec_academic_answer(bundle: dict[str, Any] | None, original_query: str | None = None) -> str:
    if not isinstance(bundle, dict) or not bundle:
        return "Chưa tìm thấy thông tin ngành, chuyên ngành hoặc chương trình đào tạo liên quan."
    if bundle.get("status") in {"empty", "major_scope_not_found", "needs_disambiguation"} and bundle.get(
        "formatted_answer"
    ):
        return str(bundle["formatted_answer"])
    if bundle.get("major_bundles"):
        return _format_multi_major_spec_academic_answer(bundle, original_query=original_query)

    major = bundle.get("major") or {}
    major_name = _display_name(major.get("name"), "ngành được hỏi")
    input_entity = bundle.get("input_entity") or {}
    input_name = _display_name(input_entity.get("name"), "") if input_entity.get("name") else None
    route = bundle.get("route")

    lines = []
    if input_name and input_name != major_name:
        lines.append(
            f"**{input_name}** thuộc phạm vi ngành **{major_name}**, nên các thông tin chương trình đào tạo liên quan được trình bày theo ngành này."
        )
    else:
        lines.append(f"**{major_name}** có các thông tin ngành và chương trình đào tạo liên quan như sau.")

    requested_attribute_answer = _format_requested_attribute_answer(bundle)
    if requested_attribute_answer:
        return requested_attribute_answer

    direct_programs = bundle.get("direct_academic_programs") or []
    if direct_programs:
        lines.append(
            f"Ngành này có **{len(direct_programs)}** chương trình đào tạo: {_join_titles(direct_programs, 'Chương trình đào tạo')}."
        )
        return "\n".join(lines)

    specializations = bundle.get("specializations") or []
    if specializations:
        total_programs = sum(len(specialization.get("academic_programs") or []) for specialization in specializations)
        lines.append(
            f"Thông tin được tổ chức theo **{len(specializations)}** chuyên ngành và **{total_programs}** chương trình đào tạo."
        )
        lines.append(f"Các chuyên ngành: {_join_titles(specializations, 'Chuyên ngành')}.")
        return "\n".join(lines)

    if route == "major_scope_not_found":
        return _SCOPE_NOT_FOUND_ANSWER

    lines.append(_NO_RELATED_ACADEMIC_ANSWER)
    return "\n".join(lines)
