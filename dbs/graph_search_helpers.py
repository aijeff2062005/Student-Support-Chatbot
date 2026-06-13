import copy
import re
import traceback
from datetime import date, datetime, time, timedelta
from typing import Any

import neo4j.time

from utils.logging_config import get_logger

logger = get_logger(__name__)

_EXCLUDE_PROPS = frozenset(
    {
        "id",
        "name",
        "embedding",
        "type",
        "code",
        "created_at",
        "updated_at",
        "version",
        "source_documents",
        "node_type",
        "elementId",
    }
)

_EXCLUDE_REL_PROPS = frozenset({"id", "embedding", "created_at", "updated_at", "source_documents"})


# ============================================================================
# TEMPORAL HELPERS
# ============================================================================


def _normalize_temporal_params(time: dict[str, Any] | None = None) -> tuple:
    """
    Normalize temporal parameters to ISO format for Neo4j datetime().

    Accepts ANY of these input formats:
      1. Already ISO: "2025-01-01T00:00:00Z" → pass through
      2. Year string: "2025" → "2025-01-01T00:00:00Z" / "2025-12-31T23:59:59Z"
      3. Year int:    2025   → same as above
      4. time dict:   {"mode": "year", "year": 2025}
                      {"mode": "range", "from_year": 2024, "to_year": 2026}

    Priority: time dict > effective_from/effective_to

    Returns:
        (effective_from_iso, effective_to_iso) — both str or None
    """
    # Priority 1: time dict (from YAML $input.time)
    if time and isinstance(time, dict):
        effective_from = time.get("from_year", None)
        effective_to = time.get("to_year", None)

        if effective_to:
            if effective_from:
                effective_to = str(int(effective_to) + 1)
            else:
                effective_to = datetime.now().strftime("%Y-%m-%d")

        # logger.error(f"Effective from {effective_from} to {effective_to}")
        return effective_from, effective_to

    return None, None


# ============================================================================
# RESULT GROUPING FUNCTIONS
# ============================================================================


def group_by_intermediate_nodes(
    direct: list[dict] | dict[str, Any],  # ← Support both formats
    indirect: list[dict] | dict[str, Any],  # ← Support both formats
    attribute_list: list[str] | None = None,
) -> dict[str, Any]:
    """
    Group enumeration results by intermediate nodes.

    SUPPORTS 2 INPUT FORMATS:

    FORMAT 1 (OLD - List):
    [
        {
            "candidate_id": "...",
            "target_id": "...",
            "candidate_properties": {...},
            "target_properties": {...},
            "intermediate_nodes": [...]
        },
        ...
    ]

    FORMAT 2 (NEW - Grouped Dict):
    {
        "target_id_1": {
            "target_id": "...",
            "target_name": "...",
            "target_properties": {...},
            "candidates": [
                {
                    "candidate_id": "...",
                    "candidate_properties": {...},
                    "intermediate_nodes": [...]
                }
            ]
        }
    }

    OUTPUT FORMAT (backward compatible):
    {
        "direct": [...],
        "grouped": {
            "Intermediate Group Name": [...]
        }
    }
    """
    try:
        result = {"direct": [], "grouped": {}}

        def normalize_to_flat_list(data: list[dict] | dict[str, Any]) -> list[dict]:
            """
            Convert both formats to unified flat list of candidates.

            Returns: [{candidate_id: ..., candidate_properties: ..., ...}, ...]
            """
            if not data:
                return []

            # FORMAT 1: Already a list
            if isinstance(data, list):
                logger.debug(f"Detected LIST format with {len(data)} items")
                return data

            # FORMAT 2: Grouped dict
            elif isinstance(data, dict):
                flat_list = []

                # Check if it's grouped format (has "candidates" key)
                is_grouped = False
                for _key, value in data.items():
                    if isinstance(value, dict) and "candidates" in value:
                        is_grouped = True
                        break

                if is_grouped:
                    # Grouped format: {target_id: {candidates: [...]}}
                    logger.debug(f"Detected GROUPED DICT format with {len(data)} target groups")

                    for target_id, target_group in data.items():
                        candidates = target_group.get("candidates", [])

                        # Add each candidate with target context
                        for candidate in candidates:
                            candidate_with_context = candidate.copy()
                            candidate_with_context["target_id"] = target_id
                            candidate_with_context["target_name"] = target_group.get("target_name")
                            candidate_with_context["target_properties"] = target_group.get("target_properties")
                            flat_list.append(candidate_with_context)

                    logger.debug(f"Flattened to {len(flat_list)} candidates")
                else:
                    # Unknown dict format - treat as empty
                    logger.warning(f"Unknown dict format: {list(data.keys())[:3]}")

                return flat_list

            return []

        def extract_entity_info(item: dict, attribute_list: list[str] | None = None) -> dict:
            """Extract entity information from candidate item."""
            candidate_props = item.get("candidate_properties", {})
            relationships_type = item.get("relationship_type", {})
            relationships_attribute = item.get("relationship_properties", {})

            entity_id = item.get("candidate_id") or candidate_props.get("id")
            entity_name = item.get("candidate_name") or candidate_props.get("name")

            # DEBUG: Log actual properties available
            logger.debug(f"extract_entity_info - candidate_props keys: {list(candidate_props.keys())}")
            logger.debug(f"extract_entity_info - attribute_list: {attribute_list}")

            if attribute_list:
                attributes = {}
                for attr in attribute_list:
                    if attr not in ["id", "name"]:
                        value = candidate_props.get(attr)
                        if value is not None:  # Only include if has actual value
                            attributes[attr] = value
                        else:
                            logger.warning(
                                f"extract_entity_info - attribute '{attr}' not found in candidate_props. Available keys: {list(candidate_props.keys())}"
                            )

                return {
                    "id": entity_id,
                    "name": entity_name,
                    "attributes": attributes,
                    "relationships_attribute": relationships_attribute,
                    "relationships_type": relationships_type,
                }
            else:
                # Return all properties except duplicates
                properties = candidate_props.copy()
                properties.pop("id", None)
                properties.pop("name", None)

                return {
                    "id": entity_id,
                    "name": entity_name,
                    "properties": properties,
                    "relationships_attribute": relationships_attribute,
                    "relationships_type": relationships_type,
                }

        # Process DIRECT connections
        direct_flat = normalize_to_flat_list(direct)
        logger.debug(f"Processing {len(direct_flat)} direct candidates")

        # DEBUG: Log first candidate's properties to verify description exists
        if direct_flat:
            first_candidate = direct_flat[0]
            first_props = first_candidate.get("candidate_properties", {})
            logger.info(f"TRACE - First direct candidate props keys: {list(first_props.keys())}")
            logger.info(f"TRACE - First direct candidate has description: {'description' in first_props}")
            logger.info(f"TRACE - attribute_list received: {attribute_list}")

        for item in direct_flat:
            entity_info = extract_entity_info(item, attribute_list)
            result["direct"].append(entity_info)

        # Process INDIRECT connections - group by intermediate nodes
        indirect_flat = normalize_to_flat_list(indirect)
        logger.debug(f"Processing {len(indirect_flat)} indirect candidates")

        for item in indirect_flat:
            intermediate_nodes = item.get("intermediate_nodes", [])

            if intermediate_nodes and len(intermediate_nodes) > 0:
                # Use first intermediate node as group key
                group_key = intermediate_nodes[0].get("node_name", "Unknown")

                if group_key not in result["grouped"]:
                    result["grouped"][group_key] = []

                entity_info = extract_entity_info(item, attribute_list)
                result["grouped"][group_key].append(entity_info)
            else:
                # No intermediate nodes - treat as direct
                entity_info = extract_entity_info(item, attribute_list)
                result["direct"].append(entity_info)

        logger.info(
            f"Grouped enumeration: {len(result['direct'])} direct, "
            f"{len(result['grouped'])} groups with "
            f"{sum(len(v) for v in result['grouped'].values())} indirect items"
        )

        return result

    except Exception as e:
        logger.error(f"Error grouping results: {e}")
        traceback.print_exc()
        return {"direct": [], "grouped": {}}


# ============================================================================
# RESULT FORMATTING FUNCTIONS
# ============================================================================


def format_attribute_search_result(search_type: str, data: Any, attribute: str | None = None) -> dict[str, Any]:
    """
    Format attribute search result for consistent response structure.

    Args:
        search_type: Type of search performed ("entity_relationship", "direct_attribute", "semantic_chunks")
        data: Result data from search
        attribute: Attribute name if applicable

    Returns:
        Formatted result dict
    """
    try:
        if search_type == "entity_relationship":
            return {"status": "success", "search_type": search_type, "results": data}

        elif search_type == "direct_attribute":
            return {"status": "success", "search_type": search_type, "value": data}

        elif search_type == "semantic_chunks":
            return {"status": "success", "search_type": search_type, "results": data}

        else:
            logger.warning(f"Unknown search type: {search_type}")
            return {"status": "error", "message": f"Unknown search type: {search_type}"}

    except Exception as e:
        logger.error(f"Error formatting attribute search result: {e}")
        return {"status": "error", "message": str(e)}


def format_enumeration_search_result(
    search_type: str, data: Any, attribute: str | None = None, total_count: int | None = None
) -> dict[str, Any]:
    """
    Format enumeration search result for consistent response structure.

    Args:
        search_type: Type of search performed ("enumeration_entities", "enumeration_attribute", "enumeration_chunks")
        data: Result data from search
        attribute: Attribute name if applicable
        total_count: Total count of results

    Returns:
        Formatted result dict
    """
    try:
        if search_type == "enumeration_entities":
            # Data should already be grouped by group_by_intermediate_nodes
            direct_counts = {
                category_key: len(category_data.get("direct", [])) for category_key, category_data in data.items()
            }
            grouped_counts = {
                category_key: sum(len(items) for items in category_data.get("grouped", {}).values())
                for category_key, category_data in data.items()
            }
            # logger.error(f"length of grouped_count: {grouped_counts}")

            return {
                "status": "success",
                "search_type": search_type,
                "total_count": {"direct_count": direct_counts, "grouped_counts": grouped_counts},
                "results": data,
            }

        elif search_type == "enumeration_attribute":
            # Data is a list of attribute values
            return {
                "status": "success",
                "search_type": search_type,
                "attribute": attribute,
                "count": len(data) if isinstance(data, list) else 1,
                "values": data if isinstance(data, list) else [data],
            }

        elif search_type == "enumeration_chunks":
            return {"status": "success", "search_type": search_type, "count": len(data), "results": data}

        else:
            logger.warning(f"Unknown search type: {search_type}")
            return {"status": "error", "message": f"Unknown search type: {search_type}"}

    except Exception as e:
        logger.error(f"Error formatting enumeration search result: {e}")
        return {"status": "error", "message": str(e)}


# ============================================================================
# VALIDATION FUNCTIONS
# ============================================================================


def format_enumeration_search_result_with_targets(search_type: str, data: dict[str, Any]) -> dict[str, Any]:
    """
    Format enumeration results with target_id context preserved.

    Returns structure:
    {
        "status": "success",
        "search_type": "enumeration_entities",
        "total_count": {
            "targets": 1,
            "node_types": {
                "Course": {
                    "direct_count": 27,
                    "grouped_count": 0
                }
            }
        },
        "results": {
            "target_id_1": {
                "target_name": "...",
                "target_properties": {...},
                "Course": {
                    "direct": [...],
                    "grouped": {}
                }
            }
        }
    }
    """
    try:
        # Calculate counts
        total_targets = len(data)
        node_type_counts = {}

        for _target_id, target_data in data.items():
            for key, value in target_data.items():
                # Skip metadata fields
                if key in ["target_id", "target_name", "target_properties"]:
                    continue

                # This is a node_type
                node_type = key
                if isinstance(value, dict):
                    direct_count = len(value.get("direct", []))
                    grouped_count = sum(len(items) for items in value.get("grouped", {}).values())

                    if node_type not in node_type_counts:
                        node_type_counts[node_type] = {"direct_count": 0, "grouped_count": 0}

                    node_type_counts[node_type]["direct_count"] += direct_count
                    node_type_counts[node_type]["grouped_count"] += grouped_count

        return {
            "status": "success",
            "search_type": search_type,
            "total_count": {"targets": total_targets, "node_types": node_type_counts},
            "results": sanitize_neo4j_types(data),
        }

    except Exception as e:
        logger.error(f"Format error: {e}")
        return {"status": "error", "message": str(e), "results": data}


def sanitize_neo4j_types(data: Any) -> Any:
    """
    Recursively convert Neo4j custom types to Python standard types.

    Handles:
    - neo4j.time.DateTime → datetime.datetime
    - neo4j.time.Date → datetime.date
    - neo4j.time.Time → datetime.time
    - neo4j.time.Duration → datetime.timedelta
    - Nested dicts, lists, and other structures

    Args:
        data: Any data structure potentially containing Neo4j types

    Returns:
        Sanitized data with only Python standard types
    """
    # Handle None
    if data is None:
        return None

    if isinstance(data, neo4j.time.DateTime):
        # logger.error(" Converting Neo4j DateTime to Python datetime")
        return datetime(
            year=data.year,
            month=data.month,
            day=data.day,
            hour=data.hour,
            minute=data.minute,
            second=data.second,
            microsecond=data.nanosecond // 1000,  # Convert nanoseconds to microseconds
            tzinfo=data.tzinfo,
        ).isoformat()

    if isinstance(data, neo4j.time.Date):
        return date(year=data.year, month=data.month, day=data.day)

    if isinstance(data, neo4j.time.Time):
        return time(
            hour=data.hour,
            minute=data.minute,
            second=data.second,
            microsecond=data.nanosecond // 1000,
            tzinfo=data.tzinfo if hasattr(data, "tzinfo") else None,
        )

    if isinstance(data, neo4j.time.Duration):
        return timedelta(days=data.days, seconds=data.seconds, microseconds=data.nanoseconds // 1000)

    # Handle dictionaries (recursive)
    if isinstance(data, dict):
        return {key: sanitize_neo4j_types(value) for key, value in data.items()}

    # Handle lists (recursive)
    if isinstance(data, list):
        return [sanitize_neo4j_types(item) for item in data]

    # Handle tuples (recursive)
    if isinstance(data, tuple):
        return tuple(sanitize_neo4j_types(item) for item in data)

    # Handle sets (recursive)
    if isinstance(data, set):
        return {sanitize_neo4j_types(item) for item in data}

    # Return as-is for standard Python types
    return data


# ============================================================================
# RELATION SEARCH HELPERS
# ============================================================================


def merge_all_entities(
    primary_entities: list[dict[str, str]], context_entities: list[dict[str, str]] | None = None
) -> dict[str, list[str]]:
    all_entities = list(primary_entities or [])
    if context_entities:
        all_entities.extend(context_entities)

    entity_names = [e.get("text", "") for e in all_entities if e.get("text")]
    entity_types = [e.get("label", "") for e in all_entities if e.get("label")]

    logger.info(f"Merged {len(entity_names)} entities: {entity_names} (types: {entity_types})")

    return {"entity_name": entity_names, "entity_type": entity_types}


__all__ = [
    "group_by_intermediate_nodes",
    "format_attribute_search_result",
    "format_enumeration_search_result",
    "format_enumeration_search_result_with_targets",
    "sanitize_neo4j_types",
    "merge_all_entities",
    "find_entity_relationships",
    "find_related_by_topic",
    "format_list_response",
    "format_count_response",
    "prepare_entities_for_search",
    "prepare_time_compare_entities_for_search",
    "build_time_compare_windows",
    "merge_search_queries",
    "find_relation_with_ids",
    "find_specialization_fee_policy_relation",
    "find_relation_by_label",
    "extract_node_ids_from_relation_attrs",
    "coalesce_time_compare_primary_entities",
    "coalesce_time_compare_compare_entities",
    "resolve_entities_individually",
    "comparison_search_found_entities",
    "resolve_academic_program_compare_rows",
    "fetch_relation_snapshot",
    "fetch_relation_contexts_for_targets",
    "merge_relation_compare_contexts",
    "merge_multi_relation_compare_contexts",
    "build_relation_compare_answer",
    "build_multi_relation_compare_answer",
]


def find_entity_relationships(node_ids: list[str], max_depth: int = 5) -> dict[str, Any] | None:
    """
    Find relationships between entities using Neo4jService.
    First node_id is used as target, rest as candidates.

    Args:
        node_ids: List of node IDs (at least 2)
        max_depth: Maximum path depth

    Returns:
        Neo4jService.find_node_relationship output
    """
    if not node_ids or len(node_ids) < 2:
        logger.warning(f"Need at least 2 node_ids, got {len(node_ids) if node_ids else 0}")
        return None

    try:
        from tools.QA.services.neo4j_service import get_neo4j_service

        svc = get_neo4j_service()

        unique_ids = list(dict.fromkeys(node_ids))

        result = svc.find_node_relationship(
            target_node_ids=[unique_ids[0]],
            candidate_node_ids=unique_ids[1:],
            max_depth=max_depth,
            include_intermediate=True,
        )

        return result

    except Exception as e:
        logger.error(f"Error finding entity relationships: {e}")
        traceback.print_exc()
        return None


def find_related_by_topic(source_node_ids: list[str], target_topic: str, max_depth: int = 3) -> dict[str, Any] | None:
    """
    Find related entities by topic using Neo4jService.find_node_relationship MODE 2 (candidate_labels).
    Reuses the same cascading search logic as what_relation.

    Args:
        source_node_ids: List of source node UUIDs (from Milvus)
        target_topic: Topic string like 'faculty', 'major', 'course', etc.
        max_depth: Maximum graph traversal depth

    Returns:
        Subgraph dict with direct_connections + indirect_connections
    """
    if not source_node_ids:
        logger.warning("find_related_by_topic: No source_node_ids provided")
        return None

    try:
        from tools.QA.services.neo4j_service import get_neo4j_service

        svc = get_neo4j_service()
        target_label = target_topic

        logger.info(
            f"find_related_by_topic: source={source_node_ids}, target_label={target_label}, max_depth={max_depth}"
        )

        result = svc.find_node_relationship(
            target_node_ids=source_node_ids,
            candidate_labels=[target_label],
            max_depth=max_depth,
            include_intermediate=True,
        )

        return result

    except Exception as e:
        logger.error(f"Error in find_related_by_topic: {e}")
        traceback.print_exc()
        return None


def format_list_response(data: list[dict[str, Any]], original_query: str = "") -> str:
    """
    Format mixed_search_list output into context for LLM.
    Only show name + description (if available). Skip metadata fields.
    Reuses same EXCLUDE_KEYS convention as format_relation_context.
    Input: [{name, id, description, labels, relationship_type, properties}, ...] (max 30 items)
    """
    if not data:
        return "Không tìm thấy thông tin liên quan."

    lines = []
    if original_query:
        lines.append(f"## Câu hỏi: {original_query}")
        lines.append("")

    lines.append(f"Tìm thấy {len(data)} kết quả:")
    lines.append("")

    for item in data:
        name = item.get("name", "Unknown")
        description = item.get("description") or item.get("properties", {}).get("description", "")

        if description:
            lines.append(f"- **{name}**: {description}")
        else:
            lines.append(f"- **{name}**")

    return "\n".join(lines)


def format_count_response(data: dict[str, Any], original_query: str = "") -> str:
    """
    Format mixed_search_count output into a count response.
    Input: {total: int, examples: [str, ...]} (examples max 30)
    """
    total = data.get("total", 0) if data else 0
    examples = data.get("examples", []) if data else []

    if total == 0:
        return "Không tìm thấy thông tin liên quan (0 kết quả)."

    lines = []
    if original_query:
        lines.append(f"## Câu hỏi: {original_query}")
        lines.append("")

    lines.append(f"Tìm thấy tổng cộng **{total}** kết quả.")

    if examples:
        lines.append(f"Danh sách (hiển thị tối đa {len(examples)}):")
        for ex in examples:
            lines.append(f"- {ex}")

    if total > len(examples):
        lines.append(f"[còn {total - len(examples)} kết quả]")

    return "\n".join(lines)


def resolve_attribute_missing_major(
    entity_names: list[str], entity_type: str, attributes: list[str], time: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """
    Resolve missing attributes for a given entity by its name and type.
    This is a fallback method when the major attribute is missing.

    Args:
        entity_names: The name of the entity to resolve.
        entity_type: The type/label of the entity (e.g., "Course", "Topic").
        attributes: List of attributes to retrieve (e.g., ["description", "name"]).
        time: Optional time dict {\"mode\": \"year\", \"year\": 2025} for temporal filtering.

    Returns:
        A dictionary of resolved attributes or None if not found.
    """
    try:
        from tools.QA.services.neo4j_service import get_neo4j_service

        svc = get_neo4j_service()
        result = svc.resolve_attribute_missing_major(
            entity_names=entity_names, entity_type=entity_type, attributes=attributes, time=time
        )
        if result:
            return result
        else:
            return None
    except Exception as e:
        logger.error(f"Error resolving missing attribute for {entity_type} '{entity_names}': {e}", exc_info=True)
        return None


def prepare_entities_for_search(entities: list[dict[str, str]], expand_types: bool = True) -> dict[str, list[str]]:
    if not entities:
        return {"entity_name": [], "entity_type": []}

    EXPAND_MAP = {
        "Major": ["Major", "Specialization"],
    }

    names = []
    types = []

    for e in entities:
        text = e.get("text", "").strip()
        label = e.get("label", "").strip()
        if text:
            names.append(text)
        if label:
            if expand_types and label in EXPAND_MAP:
                types.extend(EXPAND_MAP[label])
            else:
                types.append(label)

    # Deduplicate types while preserving order
    seen = set()
    unique_types = []
    for t in types:
        if t not in seen:
            seen.add(t)
            unique_types.append(t)

    return {"entity_name": names, "entity_type": unique_types}


def _strip_time_marker_from_text(text: str, raw_marker: str | None = None, resolved_year: int | None = None) -> str:
    if not text:
        return text

    cleaned = text
    patterns = []
    if raw_marker:
        patterns.append(re.escape(str(raw_marker).strip()))
    if resolved_year is not None:
        year_text = str(resolved_year).strip()
        patterns.extend(
            [
                rf"\bnăm\s+{re.escape(year_text)}\b",
                rf"\b{re.escape(year_text)}\b",
            ]
        )

    for pattern in patterns:
        cleaned = re.sub(pattern, " ", cleaned, flags=re.IGNORECASE)

    cleaned = re.sub(r"\bnăm\b(?=\s*$)", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.;:-")
    return cleaned or text.strip()


def prepare_time_compare_entities_for_search(
    entities: list[dict[str, str]],
    raw_marker: str | None = None,
    resolved_year: int | None = None,
    expand_types: bool = True,
) -> dict[str, list[str]]:
    normalized_entities = []
    for entity in entities or []:
        normalized_entities.append(
            {
                "label": entity.get("label", ""),
                "text": _strip_time_marker_from_text(
                    text=entity.get("text", ""),
                    raw_marker=raw_marker,
                    resolved_year=resolved_year,
                ),
            }
        )

    return prepare_entities_for_search(normalized_entities, expand_types=expand_types)


def _build_year_time_window(year: int | None) -> dict[str, int | None] | None:
    if year is None:
        return None
    return {"from_year": year, "to_year": None}


def build_time_compare_windows(
    time: dict[str, Any] | None = None,
    time_compare: dict[str, Any] | None = None,
) -> dict[str, Any]:
    time = time or {}
    time_compare = time_compare or {}

    context_year = time.get("from_year")
    left_year = time_compare.get("from_year", context_year)
    right_year = time_compare.get("to_year", time.get("to_year"))

    return {
        "context_time": _build_year_time_window(context_year),
        "left_time": _build_year_time_window(left_year),
        "right_time": _build_year_time_window(right_year),
        "left_label": str(left_year) if left_year is not None else (time_compare.get("from_raw") or "mốc 1"),
        "right_label": str(right_year) if right_year is not None else (time_compare.get("to_raw") or "mốc 2"),
    }


def _clone_found_list(found_list: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [dict(item) for item in (found_list or []) if isinstance(item, dict)]


def coalesce_time_compare_primary_entities(
    primary_found_list: list[dict[str, Any]] | None = None,
    compare_found_list: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    return _clone_found_list(primary_found_list) or _clone_found_list(compare_found_list)


def coalesce_time_compare_compare_entities(
    primary_found_list: list[dict[str, Any]] | None = None,
    compare_found_list: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    return _clone_found_list(compare_found_list) or _clone_found_list(primary_found_list)


def resolve_entities_individually(
    entity_names: list[str] | None = None,
    entity_types: str | list[str] | None = None,
    top_k: int = 1,
    threshold: float = 0.7,
    time: dict[str, Any] | None = None,
    keywords: list[str] | None = None,
) -> list[dict[str, Any]]:
    entity_names = entity_names or []
    if not entity_names:
        return []

    from dbs.milvus_helper import search_entity_by_name

    resolved = []
    for entity_name in entity_names:
        if not entity_name:
            continue
        hits = search_entity_by_name(
            entity_name=[entity_name],
            entity_type=entity_types,
            top_k=top_k,
            threshold=threshold,
            time=time,
        )
        if hits:
            resolved.append(hits[0])
    return resolved


def merge_search_queries(
    primary_texts: list[str] | None = None, keyword_attributes: list[str] | None = None
) -> list[str]:
    queries: list[str] = []
    seen: set[str] = set()

    for source in (keyword_attributes, primary_texts):
        if not source:
            continue
        for item in source:
            normalized = item.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            queries.append(normalized)

    if not queries:
        logger.warning("merge_search_queries: empty -> ['']")
        return [""]

    merged_query = ", ".join(queries)
    logger.info(f"merge_search_queries: {merged_query}")
    return [merged_query]


def fetch_relation_snapshot(
    context_found_list: list[dict[str, Any]],
    primary_found_list: list[dict[str, Any]],
    primary_topic: str = "",
    subtopics: list[str] | None = None,
    time: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not context_found_list or not primary_found_list:
        logger.warning("fetch_relation_snapshot: missing context or primary entities")
        return None

    context_ids = [item.get("node_id") for item in context_found_list if item.get("node_id")]
    primary_ids = [item.get("node_id") for item in primary_found_list if item.get("node_id")]
    if not context_ids or not primary_ids:
        logger.warning("fetch_relation_snapshot: missing node ids")
        return None

    context_label = context_found_list[0].get("node_type")
    primary_label = primary_found_list[0].get("node_type")
    is_fee_topic = primary_topic == "fee" or ("cost_fee" in (subtopics or []))

    if context_label == "Specialization" and primary_label == "Policy" and is_fee_topic:
        return find_specialization_fee_policy_relation(
            context_ids=context_ids,
            primary_ids=primary_ids,
            time=time,
        )

    if primary_label == "Course" and context_label in ["Major", "Specialization", "Faculty", "AcademicProgram"]:
        try:
            from dbs.course_admin_ops import (
                build_course_relation_fallback_depths,
                find_relation_with_ids_exact_depth,
                resolve_course_relation_depth,
            )

            resolved_depth = resolve_course_relation_depth(
                context_ids=context_ids,
                context_label=context_label,
                time=time,
            )
            if resolved_depth is not None:
                return find_relation_with_ids_exact_depth(
                    context_ids=context_ids,
                    primary_ids=primary_ids,
                    max_depth=resolved_depth,
                    fallback_depths=build_course_relation_fallback_depths(
                        context_label=context_label,
                        resolved_depth=resolved_depth,
                    ),
                    time=time,
                )
        except Exception as exc:
            logger.warning("fetch_relation_snapshot: course-specific path failed, fallback to generic path: %s", exc)

    return find_relation_with_ids(
        context_ids=context_ids,
        primary_ids=primary_ids,
        max_depth=1 if context_label == "Major" and primary_label == "Policy" else 3,
        time=time,
    )


def comparison_search_found_entities(
    primary_found_list: list[dict[str, Any]] | None = None,
    compare_found_list: list[dict[str, Any]] | None = None,
    attribute_list: list[str] | None = None,
    time: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    primary_found_list = primary_found_list or []
    compare_found_list = compare_found_list or []
    if not primary_found_list or not compare_found_list:
        return []

    from tools.QA.services.neo4j_service import get_neo4j_service

    ordered_entities = []
    seen = set()
    for item in [*primary_found_list, *compare_found_list]:
        if not isinstance(item, dict):
            continue
        node_id = item.get("node_id")
        node_type = item.get("node_type")
        if not node_id or not node_type or node_id in seen:
            continue
        seen.add(node_id)
        ordered_entities.append(item)

    if not ordered_entities:
        return []

    svc = get_neo4j_service()
    rows = svc.comparison_search_neo4j(
        node_ids=[item["node_id"] for item in ordered_entities],
        node_types=[item["node_type"] for item in ordered_entities],
        attribute_list=attribute_list,
        time=time,
    )
    if not rows:
        return []

    rows_by_id = {row.get("node_id"): row for row in rows if isinstance(row, dict) and row.get("node_id")}
    return [rows_by_id[node_id] for node_id in [item["node_id"] for item in ordered_entities] if node_id in rows_by_id]


def resolve_academic_program_compare_rows(
    found_list: list[dict[str, Any]] | None = None,
    attribute_list: list[str] | None = None,
    time: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    found_list = found_list or []
    if not found_list:
        return []

    from dbs.milvus_helper import search_entity_by_name
    from tools.QA.services.neo4j_service import get_neo4j_service

    svc = get_neo4j_service()
    rows = []

    for found in found_list:
        if not isinstance(found, dict):
            continue
        entity_name = found.get("node_name") or found.get("name")
        if not entity_name:
            continue

        program_hits = search_entity_by_name(
            entity_name=[entity_name],
            entity_type=["AcademicProgram"],
            top_k=1,
            threshold=0.5,
            time=time,
        )
        if not program_hits:
            continue

        academic_program = program_hits[0]
        program_rows = svc.comparison_search_neo4j(
            node_ids=[academic_program.get("node_id")],
            node_types=[academic_program.get("node_type")],
            attribute_list=attribute_list,
            time=time,
        )
        if not program_rows:
            continue

        properties = {}
        if isinstance(program_rows[0], dict):
            properties = dict(program_rows[0].get("properties", {}) or {})

        rows.append(
            {
                "entity": {
                    "id": found.get("node_id"),
                    "name": entity_name,
                    "type": found.get("node_type"),
                    "description": found.get("description"),
                },
                "attributes": properties,
                "source_entity": {
                    "id": academic_program.get("node_id"),
                    "name": academic_program.get("node_name"),
                    "type": academic_program.get("node_type"),
                },
            }
        )

    return rows


def fetch_relation_contexts_for_targets(
    target_found_list: list[dict[str, Any]] | None = None,
    relation_found_list: list[dict[str, Any]] | None = None,
    primary_topic: str = "",
    subtopics: list[str] | None = None,
    time: dict[str, Any] | None = None,
    original_query: str = "",
    max_candidates: int = 15,
    max_rel_samples: int = 5,
) -> list[dict[str, Any]]:
    target_found_list = target_found_list or []
    relation_found_list = relation_found_list or []
    if not target_found_list or not relation_found_list:
        return []

    entries = []
    for target in target_found_list:
        if not isinstance(target, dict):
            continue

        relation_context = {}
        subgraph = fetch_relation_snapshot(
            context_found_list=[target],
            primary_found_list=relation_found_list,
            primary_topic=primary_topic,
            subtopics=subtopics,
            time=time,
        )
        if subgraph:
            relation_context = compact_relation_results(
                subgraph=subgraph,
                original_query=original_query,
                max_candidates=max_candidates,
                max_rel_samples=max_rel_samples,
            )

        entries.append(
            {
                "slot_label": target.get("node_name") or target.get("name") or "đối tượng",
                "relation_context": relation_context or {},
                "entity": copy.deepcopy(target),
            }
        )

    return entries


def find_relation_with_ids(
    context_ids: list[str], primary_ids: list[str], max_depth: int = 3, time: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """
    Wrapper for Neo4jService.find_node_relationship MODE 1.
    context_ids = target (scope), primary_ids = candidate (what we're looking for).

    Automatically looks up PATH_REGISTRY to restrict max_depth based on
    the actual labels of context and primary nodes, preventing hop leaks
    through shared hubs (e.g., AdmissionMethod).
    """
    if not context_ids or not primary_ids:
        logger.warning("find_relation_with_ids: missing context_ids or primary_ids")
        return None

    try:
        from tools.QA.services.neo4j_service import get_neo4j_service

        svc = get_neo4j_service()

        effective_depth = max_depth
        try:
            from dbs.neo4j_helper import connect_neo4j
            from services.path_registry import lookup_path_config

            driver = connect_neo4j()
            with driver.session() as session:
                # Get labels for first context and primary node
                all_ids = [context_ids[0], primary_ids[0]]
                r = session.run(
                    "UNWIND $ids AS nid MATCH (n {id: nid}) RETURN n.id AS id, labels(n)[0] AS label",
                    ids=all_ids,
                )
                label_map = {rec["id"]: rec["label"] for rec in r}

            ctx_label = label_map.get(context_ids[0])
            pri_label = label_map.get(primary_ids[0])

            if ctx_label and pri_label:
                config = lookup_path_config(ctx_label, pri_label)
                if config:
                    effective_depth = config["depth"]
                    logger.info(
                        "find_relation_with_ids: PATH_REGISTRY (%s→%s) → max_depth=%d (was %d)",
                        ctx_label,
                        pri_label,
                        effective_depth,
                        max_depth,
                    )
        except Exception as e:
            logger.debug("find_relation_with_ids: PATH_REGISTRY lookup skipped: %s", e)

        result = svc.find_node_relationship(
            target_node_ids=context_ids,
            candidate_node_ids=primary_ids,
            max_depth=effective_depth,
            include_intermediate=True,
            time=time,
        )
        return result

    except Exception as e:
        logger.error(f"find_relation_with_ids error: {e}")
        traceback.print_exc()
        return None


def find_specialization_fee_policy_relation(
    context_ids: list[str], primary_ids: list[str], time: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """
    Resolve fee policy for Specialization with direct-relation priority.

    Priority:
      1. Highlight direct Policy-Specialization fee_information if present.
      2. Otherwise, highlight parent Major fee_information.
      3. Return merged direct + parent Major graph evidence so the answer can
         explain the requested Specialization and the Major-level fallback.
    """
    if not context_ids or not primary_ids:
        logger.warning("find_specialization_fee_policy_relation: missing context_ids or primary_ids")
        return None

    def extract_fee_information(subgraph: dict[str, Any] | None, source: str) -> dict[str, Any] | None:
        if not subgraph:
            return None

        for conn_key in ("direct_connections", "indirect_connections"):
            connections = subgraph.get(conn_key, {})
            if not isinstance(connections, dict):
                continue

            for target_group in connections.values():
                target_properties = target_group.get("target_properties") or {}
                context_info = {
                    "id": target_group.get("target_id"),
                    "name": target_group.get("target_name"),
                    "type": target_properties.get("node_type") or target_group.get("target_label"),
                }

                for candidate in target_group.get("candidates", []):
                    candidate_properties = candidate.get("candidate_properties") or {}
                    policy_info = {
                        "id": candidate.get("candidate_id"),
                        "name": candidate.get("candidate_name") or candidate_properties.get("name"),
                        "type": candidate_properties.get("node_type"),
                    }

                    rel_props = candidate.get("relationship_properties") or {}
                    fee_information = rel_props.get("fee_information")
                    if fee_information:
                        return {
                            "fee_information": fee_information,
                            "source": source,
                            "relationship_type": candidate.get("relationship_type"),
                            "context": context_info,
                            "policy": policy_info,
                        }

                    for rel_detail in candidate.get("relationship_details", []) or []:
                        detail_props = rel_detail.get("properties") or {}
                        fee_information = detail_props.get("fee_information")
                        if fee_information:
                            return {
                                "fee_information": fee_information,
                                "source": source,
                                "relationship_type": rel_detail.get("type"),
                                "context": context_info,
                                "policy": policy_info,
                            }

        return None

    def merge_subgraphs(*subgraphs: dict[str, Any] | None) -> dict[str, Any]:
        merged = {"direct_connections": {}, "indirect_connections": {}}

        for subgraph in subgraphs:
            if not subgraph:
                continue

            for conn_key in ("direct_connections", "indirect_connections"):
                connections = subgraph.get(conn_key, {})
                if not isinstance(connections, dict):
                    continue

                for target_id, target_group in connections.items():
                    candidates = list(target_group.get("candidates", []) or [])

                    if target_id not in merged[conn_key]:
                        merged[conn_key][target_id] = {
                            **target_group,
                            "candidates": candidates,
                        }
                        continue

                    existing = merged[conn_key][target_id].setdefault("candidates", [])
                    seen_relationships = {
                        item.get("relationship_id") for item in existing if item.get("relationship_id")
                    }
                    for candidate in candidates:
                        relationship_id = candidate.get("relationship_id")
                        if relationship_id and relationship_id in seen_relationships:
                            continue
                        existing.append(candidate)
                        if relationship_id:
                            seen_relationships.add(relationship_id)

        return merged

    def build_highlighted_fee_information(
        specialization_fee: dict[str, Any] | None, major_fee: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        selected_fee = specialization_fee or major_fee
        if not selected_fee:
            return None

        selected_scope = "specialization" if specialization_fee else "major"
        fee_lines = []
        if specialization_fee:
            fee_lines.append(
                {
                    "scope": "specialization",
                    "name": (specialization_fee.get("context") or {}).get("name"),
                    "fee_information": specialization_fee.get("fee_information"),
                }
            )
        if major_fee:
            fee_lines.append(
                {
                    "scope": "major",
                    "name": (major_fee.get("context") or {}).get("name"),
                    "fee_information": major_fee.get("fee_information"),
                }
            )

        return {
            "fee_lines": fee_lines,
            "selected_scope": selected_scope,
            "selected_name": (selected_fee.get("context") or {}).get("name"),
            "selected_fee_information": selected_fee.get("fee_information"),
            "specialization_name": (specialization_fee.get("context") or {}).get("name")
            if specialization_fee
            else None,
            "specialization_fee_information": specialization_fee.get("fee_information") if specialization_fee else None,
            "major_name": (major_fee.get("context") or {}).get("name") if major_fee else None,
            "major_fee_information": major_fee.get("fee_information") if major_fee else None,
        }

    try:
        from tools.QA.services.neo4j_service import get_neo4j_service

        svc = get_neo4j_service()

        direct_subgraph = svc.find_node_relationship(
            target_node_ids=context_ids,
            candidate_node_ids=primary_ids,
            max_depth=1,
            include_intermediate=True,
            time=time,
        )
        direct_fee = extract_fee_information(direct_subgraph, "direct_specialization")

        from dbs.neo4j_helper import connect_neo4j

        driver = connect_neo4j()
        with driver.session() as session:
            rows = session.run(
                """
                UNWIND $specialization_ids AS specialization_id
                MATCH (m:Major)-[:INCLUDES]-(s:Specialization {id: specialization_id})
                RETURN DISTINCT m.id AS major_id
                """,
                specialization_ids=context_ids,
            ).data()

        major_ids = [row["major_id"] for row in rows if row.get("major_id")]
        if not major_ids:
            highlighted_fee = build_highlighted_fee_information(direct_fee, None)
            if highlighted_fee:
                direct_subgraph["highlighted_fee_information"] = highlighted_fee
            return direct_subgraph

        major_subgraph = find_relation_with_ids(
            context_ids=major_ids,
            primary_ids=primary_ids,
            max_depth=1,
            time=time,
        )

        major_fee = extract_fee_information(major_subgraph, "parent_major_fallback")
        highlighted_fee = build_highlighted_fee_information(direct_fee, major_fee)

        if direct_fee:
            merged_subgraph = merge_subgraphs(direct_subgraph, major_subgraph)
        else:
            merged_subgraph = merge_subgraphs(major_subgraph, direct_subgraph)

        if highlighted_fee:
            merged_subgraph["highlighted_fee_information"] = highlighted_fee

        return merged_subgraph

    except Exception as e:
        logger.error(f"find_specialization_fee_policy_relation error: {e}")
        traceback.print_exc()
        return None


def find_relation_by_label(
    context_ids: list[str], primary_label: str, max_depth: int = 3, time: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """
    Wrapper for Neo4jService.find_node_relationship MODE 2.
    Finds ALL nodes of primary_label connected to context.
    """
    if not context_ids or not primary_label:
        logger.warning("find_relation_by_label: missing context_ids or primary_label")
        return None

    try:
        from tools.QA.services.neo4j_service import get_neo4j_service

        svc = get_neo4j_service()

        result = svc.find_node_relationship(
            target_node_ids=context_ids,
            candidate_labels=[primary_label],
            max_depth=max_depth,
            include_intermediate=True,
            time=time,
        )
        return result

    except Exception as e:
        logger.error(f"find_relation_by_label error: {e}")
        traceback.print_exc()
        return None


def extract_node_ids_from_relation_attrs(relation_attrs: list[dict[str, Any]], context_ids: list[str]) -> list[str]:
    """
    From Milvus relation attr search results, extract node IDs
    from the "other side" (not context).

    Each result has source_id + target_id. Context node is one side,
    the other side is our primary candidate.

    Deduplicates and returns unique IDs.
    """
    if not relation_attrs:
        return []

    context_set = set(context_ids or [])
    primary_ids = []
    seen = set()

    for attr in relation_attrs:
        source_id = attr.get("source_node_id", "")
        dest_id = attr.get("destination_node_id", "")

        # Pick the side that is NOT the context
        other_id = None
        if source_id in context_set and dest_id not in context_set:
            other_id = dest_id
        elif dest_id in context_set and source_id not in context_set:
            other_id = source_id
        elif source_id not in context_set:
            other_id = source_id
        elif dest_id not in context_set:
            other_id = dest_id

        if other_id and other_id not in seen:
            seen.add(other_id)
            primary_ids.append(other_id)

    logger.info(f"Extracted {len(primary_ids)} primary IDs from {len(relation_attrs)} relation attrs")
    return primary_ids


def filter_subgraph_by_keywords(
    subgraph: dict[str, Any], keywords: list[str], keyword_attributes: list[str] | None = None
) -> dict[str, Any]:
    if not subgraph or not keywords:
        return subgraph

    clean_keywords = [kw.strip() for kw in keywords if kw and kw.strip()]
    if not clean_keywords:
        return subgraph

    try:
        candidate_id_set = set()
        for conn_type in ["direct_connections", "indirect_connections"]:
            connections = subgraph.get(conn_type, {})
            if not isinstance(connections, dict):
                continue
            for target_group in connections.values():
                for candidate in target_group.get("candidates", []):
                    cid = candidate.get("candidate_id")
                    if cid:
                        candidate_id_set.add(cid)

        if not candidate_id_set:
            return subgraph

        logger.info(
            f"filter_subgraph_by_keywords: searching Milvus for {len(candidate_id_set)} "
            f"candidates with queries={clean_keywords}"
        )

        from dbs.milvus_helper import search_relation_attributes_hybrid

        milvus_results = search_relation_attributes_hybrid(
            node_ids=list(candidate_id_set), queries=clean_keywords, top_k=3, similarity_threshold=0.6
        )

        matched_ids = set()
        for r in milvus_results:
            src = r.get("source_node_id", "")
            dst = r.get("destination_node_id", "")
            # Keep the side that IS our candidate
            if src in candidate_id_set:
                matched_ids.add(src)
            if dst in candidate_id_set:
                matched_ids.add(dst)

        logger.info(
            f"Milvus returned {len(milvus_results)} hits → "
            f"{len(matched_ids)} matched candidates out of {len(candidate_id_set)}"
        )

        if not matched_ids:
            logger.warning("Milvus filter found 0 matches  returning original subgraph")
            return {}

        filtered = {}
        total_before = 0
        total_after = 0

        for conn_type in ["direct_connections", "indirect_connections"]:
            connections = subgraph.get(conn_type, {})
            if not isinstance(connections, dict):
                filtered[conn_type] = connections
                continue

            filtered_conns = {}
            for target_id, target_group in connections.items():
                candidates = target_group.get("candidates", [])
                total_before += len(candidates)

                matching = [c for c in candidates if c.get("candidate_id") in matched_ids]
                total_after += len(matching)

                if matching:
                    filtered_conns[target_id] = {
                        **{k: v for k, v in target_group.items() if k != "candidates"},
                        "candidates": matching,
                    }

            filtered[conn_type] = filtered_conns

        logger.info(f"filter_subgraph_by_keywords: {total_before}  {total_after} candidates")

        return filtered

    except Exception as e:
        logger.error(f"filter_subgraph_by_keywords error: {e}, returning original")
        traceback.print_exc()
        return subgraph


def compact_relation_results(
    subgraph: dict[str, Any], original_query: str = "", max_candidates: int = 30, max_rel_samples: int = 5
) -> dict[str, Any]:
    if not subgraph:
        return {}

    try:
        # Pattern from _extract_candidates_from_subgraph (L761)
        # but keeps ALL entries per candidate_id (not deduping yet)
        candidate_groups: dict[str, list[dict]] = {}
        context_info: dict[str, dict] = {}

        for conn_type in ["direct_connections", "indirect_connections"]:
            connections = subgraph.get(conn_type, {})
            if not isinstance(connections, dict):
                continue

            for target_id, target_group in connections.items():
                # Capture context (target = scope, e.g. University)
                if target_id not in context_info:
                    t_props = target_group.get("target_properties", {})
                    context_info[target_id] = {
                        "id": target_id,
                        "name": target_group.get("target_name", ""),
                        "type": t_props.get("node_type", ""),
                        "description": t_props.get("description", ""),
                    }

                for candidate in target_group.get("candidates", []):
                    cid = candidate.get("candidate_id")
                    if not cid:
                        continue
                    if cid not in candidate_groups:
                        candidate_groups[cid] = []
                    candidate_groups[cid].append(
                        {
                            **candidate,
                            "_conn_type": conn_type,
                            "_target_context": context_info.get(target_id, {}),
                        }
                    )

        if not candidate_groups:
            return {}

        compact_candidates = []

        for cid, entries in candidate_groups.items():
            first = entries[0]
            c_props = first.get("candidate_properties", {})

            # Clean candidate attributes (reuse EXCLUDE_KEYS pattern)
            attributes = {k: v for k, v in c_props.items() if k not in _EXCLUDE_PROPS and v is not None}
            if c_props.get("node_type") == "Course":
                attributes.pop("effective_from", None)
                attributes.pop("effective_to", None)

            raw_rels = _extract_all_relationships(entries)

            deduped_rels = _deduplicate_relationships(raw_rels, max_rel_samples)

            intermediate_names = set()
            min_hop = 999
            for entry in entries:
                for inter in entry.get("intermediate_nodes", []):
                    name = inter.get("node_name", "")
                    if name:
                        intermediate_names.add(name)
                hop = entry.get("graph_hop", 999)
                if hop < min_hop:
                    min_hop = hop

            conn_path = _build_connection_path(first)
            course_extension = {}
            if c_props.get("node_type") == "Course":
                from dbs.course_admin_ops import build_course_membership_candidate_extension

                course_extension = build_course_membership_candidate_extension(entries, attributes)
            candidate_fields = course_extension.get("candidate_fields", {})
            if course_extension:
                attributes = course_extension.get("attributes", attributes)

            compact_candidate = {
                "id": cid,
                "name": c_props.get("name", ""),
                "type": c_props.get("node_type", ""),
                "attributes": attributes,
                "graph_hop": min_hop if min_hop < 999 else 1,
                "connection_path": conn_path,
            }
            if candidate_fields:
                compact_candidate["connection_path"] = candidate_fields.get("primary_path_display") or conn_path
                compact_candidate.update(candidate_fields)
            else:
                compact_candidate["relationships"] = deduped_rels
                compact_candidate["connected_via"] = sorted(intermediate_names)[:10]

            compact_candidates.append(compact_candidate)

        compact_candidates.sort(key=_candidate_sort_key)
        compact_candidates = compact_candidates[:max_candidates]

        context_list = list(context_info.values())
        context_out = context_list[0] if len(context_list) == 1 else context_list

        result = {
            "original_query": original_query,
            "total_candidates": len(compact_candidates),
            "candidates": compact_candidates,
            "context": context_out,
        }
        if subgraph.get("highlighted_fee_information"):
            highlighted_fee = subgraph["highlighted_fee_information"]
            result["selected_fee_information"] = highlighted_fee.get("selected_fee_information")
            result["specialization_fee_information"] = highlighted_fee.get("specialization_fee_information")
            result["major_fee_information"] = highlighted_fee.get("major_fee_information")
            result["fee_information_answer"] = highlighted_fee.get("fee_lines", [])
            result["highlighted_fee_information"] = highlighted_fee

        logger.info(
            f"compact_relation_results: "
            f"{sum(len(e) for e in candidate_groups.values())} entries "
            f"→ {len(compact_candidates)} compact candidates"
        )

        return result

    except Exception as e:
        logger.error(f"compact_relation_results error: {e}")
        traceback.print_exc()
        return {}


def _relation_compare_candidate_key(candidate: dict[str, Any]) -> str:
    candidate_id = candidate.get("id")
    if candidate_id:
        return str(candidate_id)
    return f"{candidate.get('type', '')}::{candidate.get('name', '')}"


def _dedupe_relation_entries(relationships: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped = []
    seen = set()
    for relationship in relationships or []:
        if not isinstance(relationship, dict):
            continue
        attrs = relationship.get("attributes") or {}
        signature = (
            relationship.get("type"),
            tuple(sorted((str(k), str(v)) for k, v in attrs.items())),
        )
        if signature in seen:
            continue
        seen.add(signature)
        deduped.append(relationship)
    return deduped


def _coerce_relation_context_value(value: Any) -> Any:
    if isinstance(value, dict):
        if {"id", "name", "type"} & set(value.keys()):
            return {
                "id": value.get("id"),
                "name": value.get("name"),
                "type": value.get("type"),
            }
        if {"node_id", "node_name", "node_type"} & set(value.keys()):
            return {
                "id": value.get("node_id"),
                "name": value.get("node_name"),
                "type": value.get("node_type"),
            }
    if isinstance(value, list):
        normalized = [_coerce_relation_context_value(item) for item in value]
        return [item for item in normalized if item]
    return value


def _build_relation_snapshot_entries(
    base_label: str | None = None,
    base_relation_context: dict[str, Any] | None = None,
    base_entity: dict[str, Any] | None = None,
    compare_relation_entries: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    entries = []
    if base_label or base_relation_context or base_entity:
        entries.append(
            {
                "slot_label": base_label or "đối tượng gốc",
                "relation_context": base_relation_context or {},
                "entity": copy.deepcopy(base_entity) if isinstance(base_entity, dict) else {},
            }
        )

    for entry in compare_relation_entries or []:
        if not isinstance(entry, dict):
            continue
        entries.append(
            {
                "slot_label": entry.get("slot_label") or "đối tượng so sánh",
                "relation_context": entry.get("relation_context") or {},
                "entity": copy.deepcopy(entry.get("entity")) if isinstance(entry.get("entity"), dict) else {},
            }
        )
    return entries


def _merge_relation_snapshot_entries(
    snapshot_entries: list[dict[str, Any]] | None = None,
    original_query: str = "",
    snapshot_bucket_key: str = "compare_snapshots",
    relationship_label_key: str = "compare_target",
    merged_context: Any = None,
) -> dict[str, Any]:
    snapshot_entries = snapshot_entries or []
    if not snapshot_entries:
        return {}

    merged_candidates: dict[str, dict[str, Any]] = {}

    for snapshot_entry in snapshot_entries:
        if not isinstance(snapshot_entry, dict):
            continue

        slot_label = snapshot_entry.get("slot_label")
        snapshot = snapshot_entry.get("relation_context") or {}
        if not slot_label or not isinstance(snapshot, dict):
            continue

        for candidate in snapshot.get("candidates", []) or []:
            if not isinstance(candidate, dict):
                continue

            key = _relation_compare_candidate_key(candidate)
            entry = merged_candidates.setdefault(
                key,
                {
                    "id": candidate.get("id"),
                    "name": candidate.get("name", ""),
                    "type": candidate.get("type", ""),
                    "attributes": {},
                    "relationships": [],
                    "graph_hop": candidate.get("graph_hop", 999),
                },
            )

            entry["graph_hop"] = min(entry.get("graph_hop", 999), candidate.get("graph_hop", 999))

            attributes = candidate.get("attributes")
            if not isinstance(attributes, dict):
                attributes = {}

            if not entry.get("name"):
                entry["name"] = candidate.get("name", "")
            if not entry.get("type"):
                entry["type"] = candidate.get("type", "")

            for attr_key, attr_value in attributes.items():
                entry["attributes"].setdefault(attr_key, attr_value)

            entry["attributes"].setdefault(snapshot_bucket_key, {})
            entry["attributes"][snapshot_bucket_key][str(slot_label)] = {
                "attributes": copy.deepcopy(attributes),
                "relationships": copy.deepcopy(candidate.get("relationships") or []),
            }

            for relationship in candidate.get("relationships", []) or []:
                if not isinstance(relationship, dict):
                    continue
                relationship_copy = copy.deepcopy(relationship)
                relationship_attrs = relationship_copy.get("attributes")
                if not isinstance(relationship_attrs, dict):
                    relationship_attrs = {}
                relationship_attrs = dict(relationship_attrs)
                relationship_attrs[relationship_label_key] = str(slot_label)
                relationship_copy["attributes"] = relationship_attrs
                entry["relationships"].append(relationship_copy)

            if candidate.get("connection_path"):
                entry["attributes"][snapshot_bucket_key][str(slot_label)]["connection_path"] = candidate.get(
                    "connection_path"
                )
            if candidate.get("connected_via"):
                entry["attributes"][snapshot_bucket_key][str(slot_label)]["connected_via"] = candidate.get(
                    "connected_via"
                )

    merged_list = []
    for candidate in merged_candidates.values():
        candidate["relationships"] = _dedupe_relation_entries(candidate.get("relationships", []))
        merged_list.append(candidate)

    if not merged_list:
        return {}

    merged_list.sort(key=_candidate_sort_key)

    context = _coerce_relation_context_value(merged_context)
    if not context:
        for snapshot_entry in snapshot_entries:
            snapshot = snapshot_entry.get("relation_context") or {}
            context = snapshot.get("context")
            if context:
                break

    return {
        "original_query": original_query,
        "context": context,
        "total_candidates": len(merged_list),
        "candidates": merged_list,
    }


def merge_relation_compare_contexts(
    left_relation_context: dict[str, Any] | None = None,
    right_relation_context: dict[str, Any] | None = None,
    left_label: str | None = None,
    right_label: str | None = None,
    original_query: str = "",
) -> dict[str, Any]:
    entries = []
    if left_label or left_relation_context:
        entries.append({"slot_label": left_label or "mốc 1", "relation_context": left_relation_context or {}})
    if right_label or right_relation_context:
        entries.append({"slot_label": right_label or "mốc 2", "relation_context": right_relation_context or {}})

    return _merge_relation_snapshot_entries(
        snapshot_entries=entries,
        original_query=original_query,
        snapshot_bucket_key="time_compare_snapshots",
        relationship_label_key="time_point",
    )


def merge_multi_relation_compare_contexts(
    base_relation_context: dict[str, Any] | None = None,
    base_label: str | None = None,
    compare_relation_entries: list[dict[str, Any]] | None = None,
    relation_context_override: Any = None,
    original_query: str = "",
) -> dict[str, Any]:
    entries = _build_relation_snapshot_entries(
        base_label=base_label,
        base_relation_context=base_relation_context,
        compare_relation_entries=compare_relation_entries,
    )
    return _merge_relation_snapshot_entries(
        snapshot_entries=entries,
        original_query=original_query,
        snapshot_bucket_key="compare_snapshots",
        relationship_label_key="compare_target",
        merged_context=relation_context_override,
    )


_COMPARE_FACT_KEY_LABELS = {
    "cutoff_score": "điểm chuẩn",
    "quota": "chỉ tiêu",
    "fee_information": "thông tin học phí",
    "scholarship_information": "thông tin học bổng",
    "selection_method": "phương thức",
    "status": "trạng thái",
    "tuition_fee": "học phí",
    "amount": "mức",
}


def _humanize_fact_key(key: str) -> str:
    return _COMPARE_FACT_KEY_LABELS.get(key, key.replace("_", " "))


def _stringify_fact_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        return value or None
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        parts = [item.strip() if isinstance(item, str) else str(item) for item in value if item not in (None, "")]
        return ", ".join(parts[:3]) if parts else None
    if isinstance(value, dict):
        pairs = []
        for key, item in value.items():
            if key in {"effective_from", "effective_to", "time_point"} or item in (None, "", [], {}):
                continue
            rendered = _stringify_fact_value(item)
            if rendered:
                pairs.append(f"{_humanize_fact_key(str(key))}: {rendered}")
        return ", ".join(pairs[:3]) if pairs else None
    return str(value)


def _extract_candidate_summary(candidate: dict[str, Any]) -> str | None:
    name = candidate.get("name")
    facts: list[str] = []

    attributes = candidate.get("attributes") or {}
    if isinstance(attributes, dict):
        for key in _COMPARE_FACT_KEY_LABELS:
            if key in attributes:
                rendered = _stringify_fact_value(attributes.get(key))
                if rendered:
                    facts.append(f"{_humanize_fact_key(key)}: {rendered}")

    for relationship in candidate.get("relationships", []) or []:
        attrs = relationship.get("attributes") or {}
        if not isinstance(attrs, dict):
            continue
        for key in _COMPARE_FACT_KEY_LABELS:
            if key in attrs:
                rendered = _stringify_fact_value(attrs.get(key))
                if rendered:
                    time_point = attrs.get("time_point")
                    if time_point:
                        facts.append(f"{_humanize_fact_key(key)} ({time_point}): {rendered}")
                    else:
                        facts.append(f"{_humanize_fact_key(key)}: {rendered}")

    deduped_facts = list(dict.fromkeys(facts))
    if name and deduped_facts:
        return f"{name}: {', '.join(deduped_facts[:2])}"
    if name:
        return name
    if deduped_facts:
        return ", ".join(deduped_facts[:2])
    return None


def _summarize_compact_relation_context(relation_context: dict[str, Any] | None = None) -> str | None:
    relation_context = relation_context or {}

    fee_lines = relation_context.get("fee_information_answer")
    if isinstance(fee_lines, list):
        fee_summary = "; ".join(str(line).strip() for line in fee_lines[:2] if str(line).strip())
        if fee_summary:
            return fee_summary

    selected_fee_information = relation_context.get("selected_fee_information")
    rendered_fee = _stringify_fact_value(selected_fee_information)
    if rendered_fee:
        return rendered_fee

    candidate_summaries = []
    for candidate in relation_context.get("candidates", []) or []:
        summary = _extract_candidate_summary(candidate)
        if summary:
            candidate_summaries.append(summary)

    if candidate_summaries:
        return "; ".join(candidate_summaries[:3])

    return None


def _parse_numeric_like(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None

    matches = re.findall(r"\d[\d.,]*", value)
    if not matches:
        return None

    token = matches[0]
    if "." in token and "," in token:
        if token.rfind(",") > token.rfind("."):
            token = token.replace(".", "").replace(",", ".")
        else:
            token = token.replace(",", "")
    elif token.count(".") > 1 or (token.count(".") == 1 and len(token.split(".")[-1]) == 3):
        token = token.replace(".", "")
    elif token.count(",") > 1 or (token.count(",") == 1 and len(token.split(",")[-1]) == 3):
        token = token.replace(",", "")
    else:
        token = token.replace(",", ".")

    try:
        return float(token)
    except ValueError:
        return None


def _extract_first_numeric_fact(relation_context: dict[str, Any] | None = None) -> float | None:
    relation_context = relation_context or {}

    for value in relation_context.get("fee_information_answer") or []:
        numeric = _parse_numeric_like(value)
        if numeric is not None:
            return numeric

    numeric = _parse_numeric_like(relation_context.get("selected_fee_information"))
    if numeric is not None:
        return numeric

    for candidate in relation_context.get("candidates", []) or []:
        attributes = candidate.get("attributes") or {}
        if isinstance(attributes, dict):
            for key in _COMPARE_FACT_KEY_LABELS:
                numeric = _parse_numeric_like(attributes.get(key))
                if numeric is not None:
                    return numeric

        for relationship in candidate.get("relationships", []) or []:
            attrs = relationship.get("attributes") or {}
            if not isinstance(attrs, dict):
                continue
            for key in _COMPARE_FACT_KEY_LABELS:
                numeric = _parse_numeric_like(attrs.get(key))
                if numeric is not None:
                    return numeric

    return None


def _build_trend_sentence(
    left_relation_context: dict[str, Any] | None = None,
    right_relation_context: dict[str, Any] | None = None,
    left_label: str = "mốc 1",
    right_label: str = "mốc 2",
) -> str | None:
    left_value = _extract_first_numeric_fact(left_relation_context)
    right_value = _extract_first_numeric_fact(right_relation_context)
    if left_value is None or right_value is None:
        return None
    if right_value > left_value:
        return f"So với năm {left_label}, dữ liệu ở năm {right_label} cho thấy xu hướng tăng."
    if right_value < left_value:
        return f"So với năm {left_label}, dữ liệu ở năm {right_label} cho thấy xu hướng giảm."
    return f"Dữ liệu ở hai năm {left_label} và {right_label} hiện chưa cho thấy khác biệt rõ ràng."


def _render_context_name(context: Any) -> str | None:
    if isinstance(context, dict):
        return context.get("name")
    if isinstance(context, list):
        names = [item.get("name") for item in context if isinstance(item, dict) and item.get("name")]
        return ", ".join(names[:3]) if names else None
    return None


def _extract_relation_types_from_context(relation_context: dict[str, Any] | None = None) -> list[str]:
    relation_types = []
    seen_relation_types = set()
    for candidate in (relation_context or {}).get("candidates", []) or []:
        for relationship in candidate.get("relationships", []) or []:
            relation_type = relationship.get("type")
            if relation_type and relation_type not in seen_relation_types:
                seen_relation_types.add(relation_type)
                relation_types.append(relation_type)
    return relation_types


def _build_relation_compare_answer_core(
    relation_context: dict[str, Any] | None = None,
    snapshot_entries: list[dict[str, Any]] | None = None,
    intro_subject: str | None = None,
    original_query: str = "",
    mode: str = "compare",
    include_trend: bool = False,
) -> dict[str, Any]:
    relation_context = relation_context or {}
    snapshot_entries = [entry for entry in (snapshot_entries or []) if isinstance(entry, dict) and entry.get("slot_label")]
    if not snapshot_entries:
        return {}

    lines = []
    has_summary = False

    for index, snapshot_entry in enumerate(snapshot_entries):
        label = str(snapshot_entry.get("slot_label") or f"mốc {index + 1}")
        snapshot = snapshot_entry.get("relation_context") or {}
        summary = _summarize_compact_relation_context(snapshot)
        if summary:
            has_summary = True
        summary_text = summary or "chưa thấy dữ liệu phù hợp"

        if index == 0:
            intro = "Theo dữ liệu hiện có"
            if intro_subject:
                intro += f", {intro_subject}"
            if mode == "time":
                lines.append(f"{intro} ở năm {label}: {summary_text}.")
            else:
                lines.append(f"{intro} với {label}: {summary_text}.")
        else:
            prefix = "Ở năm" if mode == "time" else "Với"
            lines.append(f"{prefix} {label}: {summary_text}.")

    if include_trend and len(snapshot_entries) >= 2:
        trend_sentence = _build_trend_sentence(
            left_relation_context=snapshot_entries[0].get("relation_context"),
            right_relation_context=snapshot_entries[1].get("relation_context"),
            left_label=str(snapshot_entries[0].get("slot_label") or "mốc 1"),
            right_label=str(snapshot_entries[1].get("slot_label") or "mốc 2"),
        )
        if trend_sentence:
            lines.append(trend_sentence)

    relation_types = _extract_relation_types_from_context(relation_context)
    if not has_summary and not relation_types:
        return {}

    return {
        "formatted_answer": " ".join(lines).strip(),
        "relations": relation_types,
        "original_query": original_query,
    }


def build_relation_compare_answer(
    relation_context: dict[str, Any] | None = None,
    left_relation_context: dict[str, Any] | None = None,
    right_relation_context: dict[str, Any] | None = None,
    primary_found_list: list[dict[str, Any]] | None = None,
    compare_found_list: list[dict[str, Any]] | None = None,
    time_windows: dict[str, Any] | None = None,
    original_query: str = "",
) -> dict[str, Any]:
    relation_context = relation_context or {}
    time_windows = time_windows or {}

    left_label = str(time_windows.get("left_label", "mốc 1"))
    right_label = str(time_windows.get("right_label", "mốc 2"))
    subject_name = _render_context_name(relation_context.get("context"))
    object_name = None
    for item in (primary_found_list or []):
        if isinstance(item, dict) and item.get("node_name"):
            object_name = item["node_name"]
            break
    if object_name is None:
        for item in (compare_found_list or []):
            if isinstance(item, dict) and item.get("node_name"):
                object_name = item["node_name"]
                break

    intro = None
    if object_name and subject_name:
        intro = f"{object_name} trong phạm vi {subject_name}"
    elif object_name:
        intro = object_name
    elif subject_name:
        intro = f"trong phạm vi {subject_name}"

    return _build_relation_compare_answer_core(
        relation_context=relation_context,
        snapshot_entries=[
            {"slot_label": left_label, "relation_context": left_relation_context or {}},
            {"slot_label": right_label, "relation_context": right_relation_context or {}},
        ],
        intro_subject=intro,
        original_query=original_query,
        mode="time",
        include_trend=True,
    )


def build_multi_relation_compare_answer(
    relation_context: dict[str, Any] | None = None,
    base_label: str | None = None,
    base_relation_context: dict[str, Any] | None = None,
    compare_relation_entries: list[dict[str, Any]] | None = None,
    relation_found_list: list[dict[str, Any]] | None = None,
    original_query: str = "",
) -> dict[str, Any]:
    carrier_name = None
    for item in relation_found_list or []:
        if isinstance(item, dict) and item.get("node_name"):
            carrier_name = item["node_name"]
            break

    entries = _build_relation_snapshot_entries(
        base_label=base_label,
        base_relation_context=base_relation_context,
        compare_relation_entries=compare_relation_entries,
    )

    return _build_relation_compare_answer_core(
        relation_context=relation_context,
        snapshot_entries=entries,
        intro_subject=carrier_name,
        original_query=original_query,
        mode="compare",
        include_trend=False,
    )


def _extract_all_relationships(entries: list[dict]) -> list[dict]:
    rels = []

    for entry in entries:
        rel_type = entry.get("relationship_type")
        rel_props = entry.get("relationship_properties")

        if rel_type:
            clean_attrs = {k: v for k, v in (rel_props or {}).items() if k not in _EXCLUDE_REL_PROPS and v is not None}
            rels.append(
                {
                    "type": rel_type,
                    "attributes": clean_attrs,
                    "target_name": "",  # direct has no intermediate
                }
            )

        rel_details = entry.get("relationship_details", [])
        if rel_details:
            for rd in rel_details:
                clean_attrs = {
                    k: v for k, v in rd.get("properties", {}).items() if k not in _EXCLUDE_REL_PROPS and v is not None
                }
                rels.append(
                    {
                        "type": rd.get("type", "RELATED"),
                        "attributes": clean_attrs,
                        "target_name": rd.get("target_name", ""),
                    }
                )

    return rels


def _deduplicate_relationships(raw_rels: list[dict], max_samples: int = 5) -> list[dict]:
    if not raw_rels:
        return []

    # Group by (type, attribute_signature)
    groups: dict[tuple, dict] = {}

    for rel in raw_rels:
        rtype = rel.get("type", "RELATED")
        attrs = rel.get("attributes", {})

        # Signature = type + sorted attr key-value pairs
        sig = (rtype, tuple(sorted((k, str(v)[:100]) for k, v in attrs.items())))

        if sig not in groups:
            groups[sig] = {"type": rtype, "attributes": attrs, "targets": [], "count": 0}

        target_name = rel.get("target_name", "")
        if target_name:
            groups[sig]["targets"].append(target_name)
        groups[sig]["count"] += 1

    # Build output, sorted by count DESC
    result = []
    sorted_groups = sorted(groups.values(), key=lambda x: -x["count"])

    for g in sorted_groups[: max_samples * 3]:  # generous cap, will be grouped later
        entry = {"type": g["type"], "attributes": g["attributes"]}

        # Only add count/sample_targets if duplicates exist (compacted)
        if g["count"] > 1:
            entry["count"] = g["count"]
            unique_targets = list(dict.fromkeys(g["targets"]))[:3]
            if unique_targets:
                entry["sample_targets"] = unique_targets

        result.append(entry)

    return result


def _build_connection_path(candidate_entry: dict) -> str:
    """
    Build human-readable connection path using node types.
    E.g. "Policy →[APPLIES_TO]→ Major →[TRAINS]→ Faculty →[MANAGES]→ University"

    Uses candidate_properties.node_type + intermediate_nodes[].node_type
    instead of instance names, since names repeat across paths.
    """
    c_props = candidate_entry.get("candidate_properties", {})
    intermediates = candidate_entry.get("intermediate_nodes", [])
    rel_types = candidate_entry.get("relationship_types", [])

    if not rel_types:
        return ""

    parts = [c_props.get("node_type", "?")]

    for i, rtype in enumerate(rel_types):
        parts.append(f"[{rtype}]")
        if i < len(intermediates):
            parts.append(intermediates[i].get("node_type", "?"))

    return " → ".join(parts)


def _candidate_sort_key(candidate: dict):
    """
    Sort key: effective_from DESC (newest first), then graph_hop ASC (closest first).
    Candidates with effective_from come before those without.

    effective_from lives inside relationships[].attributes.effective_from
    (NOT candidate["properties"] — that key doesn't exist in compact output).
    We pick the MAX (newest) effective_from across all relationships.
    """
    # Scan all relationships for the newest effective_from
    # ef = None
    # for rel in candidate.get("relationships", []):
    #     val = rel.get("attributes", {}).get("effective_from", None)
    #     logger.error(f"Checking relationship {rel.get('type')} with effective_from={val} for candidate {candidate.get('name')}")
    #     if ef is None or (val and val > ef):
    #         ef = val

    hop = candidate.get("graph_hop", 999)
    # Negate: has_date first (1 before 0), then reverse string for DESC
    # has_date = 1 if ef else 0
    # return -has_date, ef if not ef else _negate_date_str(ef), hop
    return hop


def _negate_date_str(date_str: str) -> str:
    """Negate date string for reverse sorting. '2025' → '7974'."""
    try:
        return "".join(chr(ord("9") - ord(c)) if c.isdigit() else c for c in date_str)
    except Exception:
        return date_str
