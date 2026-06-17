
import re as _re
import traceback
from typing import Any

from dbs.graph_search_helpers import _normalize_temporal_params
from tools.QA.services.neo4j_service import build_temporal_where_clause
from utils.logging_config import get_logger

logger = get_logger(__name__)

# Shared hubs can create cross-branch leakage when used as intermediate hops.
# Keep endpoints valid; only block these labels when they appear in the middle of a path.
BLOCKED_INTERMEDIATE_LABELS = ("EducationSystem",)

DESCRIPTION_ALIAS_KEYS = [
    "description",
    "bio",
    "selection_method",
    "address",
    "combination_detail",
    "role",
    "Group",
    "position",
    "honor",
    "degree",
    "gender",
]

# Labels that should always be searched together
_EXPAND_TYPE_MAP = {
    "Major": ["Major", "Specialization"],
    "Specialization": ["Specialization", "Major"],
}
_EXCLUDE_PROPS = {
    "id",
    "embedding",
    "type",
    "code",
    "created_at",
    "updated_at",
    "version",
    "source_documents",
    "elementId",
}
_PERSON_CORE_FIELDS = {"gender", "degree", "academic_rank", "bio"}

_EXCLUDE_REL_PROPS = {"id", "embedding", "created_at", "updated_at", "source_documents"}
_CUTOFF_SIGNALS = {
    "điểm chuẩn",
    "điểm xét tuyển",
    "điểm trúng tuyển",
    "cutoff_score",
    "cutoff score",
    "admission cutoff",
    "passing score",
    "entry score",
}
_CUTOFF_OR_QUOTA_SIGNALS = _CUTOFF_SIGNALS | {"điểm sàn", "điểm đầu vào", "chỉ tiêu", "quota"}
_CUTOFF_REL_ATTR_KEYS = ("cutoff_score", "estimated_score", "quota", "effective_from", "effective_to", "status")
_CUTOFF_ATTR_QUERY_SIGNALS = _CUTOFF_SIGNALS | {"cutoff_score", "cutoff score"}
_QUOTA_ATTR_QUERY_SIGNALS = {"chỉ tiêu", "quota", "admission quota"}


def _has_nonempty_value(value: Any) -> bool:
    return value is not None and str(value).strip() != ""


def _public_rel_properties(props: dict[str, Any] | None) -> dict[str, Any]:
    return {key: value for key, value in dict(props or {}).items() if key not in _EXCLUDE_REL_PROPS}


def _extract_connected_via_names(intermediate_nodes: list[dict[str, Any]] | None) -> list[str]:
    names: list[str] = []
    for node in intermediate_nodes or []:
        name = str(node.get("node_name") or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def _is_relation_definition_label(label: str | None) -> bool:
    label = str(label or "").strip()
    return bool(label) and label == label.upper()


def filter_attribute_definitions(
    resolved_attr_defs: list[dict[str, Any]] | None = None,
    definition_kind: str = "node",
    allowed_labels: list[str] | None = None,
) -> list[dict[str, Any]]:
    allowed_label_set = set(allowed_labels or [])
    want_relation = definition_kind == "relation"
    filtered: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for definition in resolved_attr_defs or []:
        if not isinstance(definition, dict):
            continue
        label = str(definition.get("label") or "").strip()
        attr_name = str(definition.get("attribute_name") or "").strip()
        if not label or not attr_name:
            continue
        is_relation = _is_relation_definition_label(label)
        if is_relation != want_relation:
            continue
        if allowed_label_set and label not in allowed_label_set:
            continue
        key = (label, attr_name)
        if key in seen:
            continue
        seen.add(key)
        filtered.append(definition)

    return filtered


def attribute_names_from_definitions(
    resolved_attr_defs: list[dict[str, Any]] | None = None,
    extra_attribute_names: list[str] | None = None,
) -> list[str]:
    names: list[str] = []
    for definition in resolved_attr_defs or []:
        if isinstance(definition, dict) and definition.get("attribute_name"):
            names.append(str(definition["attribute_name"]))
    names.extend(str(name) for name in extra_attribute_names or [] if name)
    return list(dict.fromkeys(name.strip() for name in names if name and name.strip()))


def _exact_attribute_definition_lookup(
    keywords: list[str],
    label: str | None = None,
    collection_name: str = "knowledge_attribute_definitions",
    top_k: int = 5,
) -> list[dict[str, Any]]:
    clean_keywords = list(dict.fromkeys(str(keyword).strip() for keyword in keywords if str(keyword).strip()))
    exact_attr_names = [keyword for keyword in clean_keywords if keyword.replace("_", "").isalnum()]
    if not exact_attr_names:
        return []

    from pymilvus import Collection, utility

    from dbs.milvus_helper import connect_milvus

    if not connect_milvus() or not utility.has_collection(collection_name):
        return []

    output_fields = [
        "label",
        "attribute_name",
        "attribute_description",
        "attribute_keywords",
    ]
    quoted_names = ",".join('"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"' for name in exact_attr_names)
    exact_expr = f"attribute_name in [{quoted_names}]"
    if label:
        exact_expr = f'({exact_expr}) and (label == "{label}")'

    collection = Collection(name=collection_name)
    collection.load()
    exact_rows = collection.query(
        expr=exact_expr,
        output_fields=output_fields,
        limit=max(top_k, len(exact_attr_names)),
    )
    exact_definitions = [
        {
            "label": row.get("label", ""),
            "attribute_name": row.get("attribute_name", ""),
            "attribute_description": row.get("attribute_description", ""),
            "attribute_keywords": row.get("attribute_keywords", []),
            "similarity_score": 1.0,
        }
        for row in exact_rows or []
        if row.get("attribute_name")
    ]
    exact_definitions.sort(key=lambda item: (item["attribute_name"], item["label"]))
    return exact_definitions[:top_k]


def search_constraint_attribute_definitions(
    keywords: list[str],
    label: str | None = None,
    collection_name: str = "knowledge_attribute_definitions",
    similarity_threshold: float = 0.5,
    top_k: int = 5,
) -> list[dict[str, Any]]:
    """
    Constraint-list local attribute resolver.

    It checks exact `attribute_name` tokens first because query_agent often
    already emits canonical keys like `cutoff_score`, `quota`, or
    `total_credits`. The generic Milvus helper remains semantic-only so other
    query families keep their existing ranking behavior.
    """
    try:
        exact_matches = _exact_attribute_definition_lookup(
            keywords=keywords,
            label=label,
            collection_name=collection_name,
            top_k=top_k,
        )
    except Exception as exc:
        logger.warning("exact constraint attribute lookup failed: %s", exc)
        exact_matches = []
    if exact_matches:
        return exact_matches

    from dbs.milvus_helper import search_attribute_definitions

    return search_attribute_definitions(
        keywords=keywords,
        label=label,
        collection_name=collection_name,
        similarity_threshold=similarity_threshold,
        top_k=top_k,
    )


def build_relation_constraint_queries(
    primary_texts: list[str] | None = None,
    keyword_attributes: list[str] | None = None,
    resolved_relation_attr_defs: list[dict[str, Any]] | None = None,
) -> list[str]:
    relation_attr_names = attribute_names_from_definitions(resolved_relation_attr_defs)
    return merge_constraint_search_queries(
        primary_texts=[*(primary_texts or []), *relation_attr_names],
        keyword_attributes=keyword_attributes,
    )


def _constraint_list_result(
    items: list[dict[str, Any]],
    source: str,
    display_limit: int = 5,
    start_index: int = 0,
    **extra: Any,
) -> dict[str, Any]:
    total = len(items)
    result = {
        "items": items[start_index : start_index + display_limit],
        "total": total,
        "source": source,
        "display_limit": display_limit,
        "start_index": start_index,
        "list_last_index": min(start_index + display_limit, total),
    }
    result.update({key: value for key, value in extra.items() if value is not None})
    return result


# Internal: shared Cypher query builder


def _query_related_nodes(
    source_node_ids: list[str] | None,
    target_topics: list[str],
    max_depth: int = 3,
    limit: int | None = None,
    only_min_hop_layer: bool = True,
    effective_from: str | None = None,
    effective_to: str | None = None,
) -> list[dict[str, Any]]:
    """
    Generic multi-hop query: find all DISTINCT nodes of target_topics
    reachable from source nodes.

    Delegates to Neo4jService._find_shortest_path (allShortestPaths algorithm)
    when source_node_ids is provided. Falls back to simple MATCH (b:Label)
    when source_node_ids is None (return ALL nodes of label).

    Post-processing:
    → Blocks paths that pass through BLOCKED_INTERMEDIATE_LABELS (EducationSystem)
    → _find_shortest_path already applies _filter_candidates_by_min_hop
    → Converts grouped_results output to flat list format

    Args:
        source_node_ids: List of source UUIDs. None = query ALL nodes of target label.
        target_topics: e.g. ["Major", "Course", "Faculty"]
        max_depth: Max relationship hops (default 3)
        limit: Max results to return. None = return ALL (for counting).
        only_min_hop_layer: Ignored — _find_shortest_path always applies min_hop filter.
        effective_from: ISO UTC start of validity window (optional)
        effective_to: ISO UTC end of validity window (optional)

    Returns:
        List[Dict] with keys: name, id, description, labels, properties
    """

    if source_node_ids:
        # Mapping:
        from tools.QA.services.neo4j_service import get_neo4j_service

        svc = get_neo4j_service()

        logger.critical(f"list_via_subgraph: target_topics = {target_topics}")
        grouped_results = svc._find_shortest_path(
            target_node_ids=source_node_ids,
            candidate_labels=target_topics,
            max_depth=max_depth,
            include_intermediate=True,
            effective_from=effective_from,
            effective_to=effective_to,
            apply_temporal_to_intermediate=False,
            need_to_use_min_hop=False if target_topics == ["Major", "Specialization"] else True,
        )
        logger.critical(
            f"_query_related_nodes: _find_shortest_path returned {len(grouped_results)} target groups for source_node_ids={source_node_ids} and target_topics={target_topics}"
        )

        # _find_shortest_path does NOT filter intermediate labels, so we do it here.
        blocked = set(BLOCKED_INTERMEDIATE_LABELS)
        for _target_id, target_group in list(grouped_results.items()):
            filtered_candidates = []
            for candidate in target_group.get("candidates", []):
                intermediate_nodes = candidate.get("intermediate_nodes", [])
                # Check if any intermediate node has a blocked label
                has_blocked = any(node.get("node_type", "") in blocked for node in intermediate_nodes)
                if not has_blocked:
                    filtered_candidates.append(candidate)
            target_group["candidates"] = filtered_candidates

        # Remove target groups with no candidates left after filtering
        grouped_results = {tid: tg for tid, tg in grouped_results.items() if tg.get("candidates")}

        # _find_shortest_path returns: {target_id: {candidates: [{candidate_id, candidate_properties, ...}]}}
        # We need: [{name, id, description, labels, properties, relationship_type, relationship_properties}]
        rows_by_id: dict[str, dict[str, Any]] = {}
        for target_group in grouped_results.values():
            for candidate in target_group.get("candidates", []):
                cid = candidate.get("candidate_id")
                if not cid:
                    continue

                # Extract Group values from this candidate's relationship details
                rel_details = candidate.get("relationship_details", [])
                candidate_groups = []
                for rd in rel_details:
                    props = rd.get("properties", {}) if rd else {}
                    groups = props.get("Group", [])
                    if isinstance(groups, str):
                        groups = [groups]
                    candidate_groups.extend(groups)
                connected_via = _extract_connected_via_names(candidate.get("intermediate_nodes", []))

                if cid in rows_by_id:
                    existing_groups = rows_by_id[cid].get("_all_groups", [])
                    for g in candidate_groups:
                        if g not in existing_groups:
                            existing_groups.append(g)
                    rows_by_id[cid]["_all_groups"] = existing_groups
                    existing_connected_via = rows_by_id[cid].get("connected_via", [])
                    rows_by_id[cid]["connected_via"] = existing_connected_via + [
                        name for name in connected_via if name not in existing_connected_via
                    ]
                    continue

                c_props = candidate.get("candidate_properties", {})
                c_labels = [c_props.get("node_type", "")] if c_props.get("node_type") else []

                # Preserve relationship data from the first path
                first_rel = rel_details[0] if rel_details else {}
                rel_type = first_rel.get("type", "") if first_rel else ""
                rel_props = dict(first_rel.get("properties", {})) if first_rel and first_rel.get("properties") else {}

                rows_by_id[cid] = {
                    "name": candidate.get("candidate_name", "") or c_props.get("name", ""),
                    "id": cid,
                    "description": c_props.get("description", "") or "",
                    "labels": c_labels,
                    "relationship_type": rel_type,
                    "relationship_properties": rel_props,
                    "properties": dict(c_props),
                    "connected_via": connected_via,
                    "_all_groups": candidate_groups,
                }

        rows = sorted(rows_by_id.values(), key=lambda x: x.get("name") or "")
        if limit:
            rows = rows[:limit]

    else:
        from dbs.neo4j_helper import connect_neo4j

        driver = connect_neo4j()
        target_label_expr = "|".join(target_topics)
        temporal_clause = build_temporal_where_clause("b", effective_from, effective_to)
        where_part = f"WHERE {temporal_clause}" if temporal_clause else ""

        query = f"""
        MATCH (b:{target_label_expr})
        {where_part}
        RETURN b.name  AS name,
               b.id    AS id,
               b.description AS description,
               labels(b)     AS labels,
               properties(b) AS properties
        ORDER BY b.name ASC
        """
        if limit:
            query += f"LIMIT {limit}"

        params: dict[str, Any] = {}
        if effective_from:
            params["effective_from"] = effective_from
        if effective_to:
            params["effective_to"] = effective_to

        with driver.session() as session:
            result = session.run(query, **params)
            rows = []
            for r in result:
                rows.append(
                    {
                        "name": r["name"],
                        "id": r["id"],
                        "description": r["description"] or "",
                        "labels": list(r["labels"]) if r["labels"] else [],
                        "relationship_type": "",
                        "relationship_properties": {},
                        "properties": dict(r["properties"]) if r["properties"] else {},
                    }
                )

    logger.info(
        f"_query_related_nodes: {len(rows)} DISTINCT {','.join(target_topics)} "
        f"(source={source_node_ids}, depth={max_depth}, limit={limit})"
    )
    return rows


# Public API: list (used by YAML flows)


def _find_intermediate_nodes(
    source_node_ids: list[str] | None, target_topics: list[str], all_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """
    Find intermediate nodes between source and target using the existing
    find_node_relationship service.

    - If candidate_ids exist (from all_rows) → MODE 1 (candidate_node_ids)
    - If not → MODE 2 (candidate_labels from target_topics)

    Example: source=University, target_topics=["major"]
    → MODE 2: candidate_labels=["Major"]
    → intermediate = Faculty (Khoa CNTT, Khoa QTKD, ...)

    Args:
        source_node_ids: Source UUIDs
        target_topics: e.g. ["major", "course"]
        all_rows: List of target node dicts with "id" keys

    Returns:
        List[Dict] with keys: name, id, type (sorted by name)
    """
    from tools.QA.services.neo4j_service import get_neo4j_service

    if not source_node_ids:
        return []

    svc = get_neo4j_service()

    # Choose MODE based on available candidate IDs
    candidate_ids = [r["id"] for r in all_rows if r.get("id")]

    try:
        if candidate_ids:
            result = svc.find_node_relationship(
                target_node_ids=source_node_ids,
                candidate_node_ids=candidate_ids,
                max_depth=3,
                include_intermediate=True,
            )
        else:
            result = svc.find_node_relationship(
                target_node_ids=source_node_ids, candidate_labels=target_topics, max_depth=3, include_intermediate=True
            )

        # Extract unique intermediate nodes from indirect_connections
        seen = set()
        intermediates = []
        # logger.error(f"_find_intermediate_nodes result: {result}")
        indirect = result.get("indirect_connections", {})
        # logger.error(f"_find_intermediate_nodes: found {intermediates} ")
        for target_group in indirect.values():
            for candidate in target_group.get("candidates", []):
                # logger.error(f"Error in _find_intermediate_nodes: {candidate}")
                for node in candidate.get("intermediate_nodes", []):
                    nid = node.get("node_id")
                    if nid and nid not in seen:
                        seen.add(nid)
                        intermediates.append({"name": node.get("node_name"), "id": nid, "type": node.get("node_type")})

        logger.info(
            f"_find_intermediate_nodes: found {len(intermediates)} unique "
            f"intermediate nodes (source={source_node_ids}, topics={target_topics})"
        )
        return sorted(intermediates, key=lambda x: x.get("name", ""))

    except Exception as e:
        logger.error(f"Error in _find_intermediate_nodes: {e}")
        traceback.print_exc()
        return []


def list_via_subgraph(
    source_node_ids: list[str],
    target_topics: list[str],
    max_depth: int = 3,
    limit: int = 30,
    display_limit: int = 5,
    start_index: int = 0,
    time: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    List DISTINCT B nodes reachable from source A nodes.
    Queries ALL matching nodes (sorted alphabetically by name).
    Returns all items + pagination metadata for display limiting.

    Args:
        source_node_ids: Source UUIDs (from Milvus search)
        target_topics: e.g. ["major", "course", "faculty"]
        max_depth: Max hops (default 5)
        limit: Max results to query from Neo4j
        display_limit: Max results to display in formatted output (default 5)
        start_index: Starting index for pagination (0 for first page, list_last_index for next)
        time: Optional time dict {"mode": "year", "year": 2025}

    Returns:
        {
            "items": List[Dict],
            "total": int,
            "display_limit": int,
            "start_index": int,
            "list_last_index": int,
            "intermediate_nodes": List[Dict]
        }
    """
    try:
        effective_from, effective_to = _normalize_temporal_params(time)
        if "Major" in target_topics and "Specialization" not in target_topics:
            target_topics.append("Specialization")

        all_rows_combined = _query_related_nodes(
            source_node_ids=source_node_ids,
            target_topics=target_topics,
            max_depth=max_depth,
            limit=None,
            effective_from=effective_from,
            effective_to=effective_to,
        )

        intermediate_nodes_combined = _find_intermediate_nodes(
            source_node_ids=source_node_ids, target_topics=target_topics, all_rows=all_rows_combined
        )

        # Re-sort combined rows by name ASC
        all_rows_combined.sort(key=lambda x: x.get("name", ""))
        logger.critical(f"list_via_subgraph: total {len(all_rows_combined)} items before pagination")

        total = len(all_rows_combined)
        list_last_index = min(start_index + display_limit, total)

        logger.info(
            f"list_via_subgraph: found {total} DISTINCT items for topics "
            f"'{target_topics}' (start={start_index}, display_limit={display_limit}, "
            f"list_last_index={list_last_index}, "
            f"intermediates={len(intermediate_nodes_combined)}, source: {source_node_ids})"
        )
        return {
            "items": all_rows_combined,
            "total": total,
            "display_limit": display_limit,
            "start_index": start_index,
            "list_last_index": list_last_index,
            "intermediate_nodes": intermediate_nodes_combined,
        }

    except Exception as e:
        logger.error(f"Error in list_via_subgraph: {e}")
        traceback.print_exc()
        return {
            "items": [],
            "total": 0,
            "display_limit": display_limit,
            "start_index": start_index,
            "list_last_index": 0,
            "intermediate_nodes": [],
        }


def intersect_pool_with_entities(
    candidate_pool: dict[str, Any] | None,
    entity_search_results: list[dict[str, Any]] | None,
    display_limit: int = 5,
    start_index: int = 0,
) -> dict[str, Any] | None:
    """
    Intersect candidate_pool with search_entity_by_name results.

    Pure join layer — no searching. Takes pre-computed entity search results
    (from search_entity_by_name) and filters candidate_pool to only items
    that appear in those results. Attaches relevance score for display.

    Args:
        candidate_pool: Output of list_via_subgraph (has .items[].id, .items[].name)
        entity_search_results: Output of search_entity_by_name
            Each item: {node_id, node_name, node_type, score, ...}
        display_limit: Pagination limit
        start_index: Pagination offset

    Returns:
        Filtered pool dict (same shape as list_via_subgraph output) or None if no matches.
        Each item gets matched_attributes = {"relevance": "<name> (score: X.XX)"}
    """
    if not candidate_pool or not entity_search_results:
        logger.info("intersect_pool_with_entities: empty pool or search results  None")
        return None

    pool_items = candidate_pool.get("items", [])
    if not pool_items:
        logger.info("intersect_pool_with_entities: pool has 0 items  None")
        return None

    pool_id_to_item = {item["id"]: item for item in pool_items if item.get("id")}
    pool_id_set = set(pool_id_to_item.keys())

    # Intersect: keep only entities that exist in the pool
    matched_items = []
    for entity in entity_search_results:
        eid = entity.get("node_id")
        if eid in pool_id_set:
            item = dict(pool_id_to_item[eid])  # shallow copy
            score = entity.get("score", 0.0)
            search_name = entity.get("node_name", "")
            item["matched_attributes"] = {
                "relevance": f"{search_name} (điểm tương đồng: {score:.2f})",
            }
            item["_entity_search_score"] = score
            matched_items.append(item)

    if not matched_items:
        logger.info(
            f"intersect_pool_with_entities: {len(entity_search_results)} search results "
            f"but none in pool ({len(pool_id_set)} items) → None"
        )
        return None

    # Sort by relevance score desc
    matched_items.sort(key=lambda x: x.get("_entity_search_score", 0), reverse=True)
    total = len(matched_items)
    list_last_index = min(start_index + display_limit, total)

    logger.info(
        f"intersect_pool_with_entities: {total} pool items matched "
        f"(top: {matched_items[0]['name']} score={matched_items[0].get('_entity_search_score', 0):.3f})"
    )
    return {
        "items": matched_items,
        "total": total,
        "source": "entity_filter",
        "display_limit": display_limit,
        "start_index": start_index,
        "list_last_index": list_last_index,
    }


# Formatting: list with total count


def _format_list_entity_refs(entities: list[dict[str, Any]] | None) -> str:
    if not entities:
        return ""

    names: list[str] = []
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        name = entity.get("name") or entity.get("text") or entity.get("node_name")
        if name:
            names.append(str(name))

    return ", ".join(dict.fromkeys(names))


def format_list_with_total(
    data: dict[str, Any],
    original_query: str = "",
    context_entities: list[dict[str, Any]] | None = None,
    context_found_list: list[dict[str, Any]] | None = None,
) -> str:
    """
    Format list results with total count, limited display, and pagination info.

    Input: {
        "items": [{"name":..., "description":...}, ...],
        "total": int,
        "display_limit": int,
        "list_last_index": int,
        "intermediate_nodes": List[Dict]
    }
    Output: Markdown with total count + bullet list of display_limit items.
    Format similar to what_count but with bullet points.
    """
    if not data:
        return "Không tìm thấy thông tin liên quan."

    items = data.get("items", [])
    total = data.get("total", len(items))
    display_limit = data.get("display_limit", 5)

    if total == 0:
        return "Không tìm thấy thông tin liên quan (0 kết quả)."

    start_index = data.get("start_index", 0)

    def _slice_display_items(rows: list[dict[str, Any]], offset: int, limit: int):
        if not rows or offset >= len(rows) or limit <= 0:
            return [], min(offset, len(rows))

        labels = {(row.get("labels") or [""])[0] for row in rows}
        has_mixed_major_spec = "Major" in labels and "Specialization" in labels

        if not has_mixed_major_spec:
            next_index = min(offset + limit, len(rows))
            return rows[offset:next_index], next_index

        display_rows = []
        cursor = offset
        major_count = 0

        while cursor < len(rows) and major_count < limit:
            row = rows[cursor]
            label = (row.get("labels") or [""])[0]
            display_rows.append(row)
            cursor += 1

            if label != "Major":
                major_count += 1
                continue

            major_count += 1
            parent_major_id = row.get("id")
            parent_major_name = row.get("name")

            while cursor < len(rows):
                child = rows[cursor]
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

        return display_rows, cursor

    display_items, next_raw_index = _slice_display_items(items, start_index, display_limit)
    data["list_last_index"] = next_raw_index
    remaining = max(total - next_raw_index, 0)

    lines = []
    if original_query:
        lines.append(f"## Câu hỏi: {original_query}")
        lines.append("")
    asked_context_text = _format_list_entity_refs(context_entities)
    resolved_context_text = _format_list_entity_refs(context_found_list)
    if asked_context_text or resolved_context_text:
        if asked_context_text:
            lines.append(f"Người dùng hỏi về: **{asked_context_text}**.")
        if resolved_context_text:
            lines.append(f"Dưới đây là thông tin tìm được cho: **{resolved_context_text}**.")
        lines.append("")

    # show breakdown so user understands the distinction.
    _LABEL_VN = {"Major": "ngành", "Specialization": "chuyên ngành"}
    label_counts: dict[str, int] = {}
    for item in items:
        lbl = (item.get("labels") or [""])[0]
        if lbl in _LABEL_VN:
            label_counts[lbl] = label_counts.get(lbl, 0) + 1

    if len(label_counts) >= 2:
        ordered_labels = list(label_counts.keys())
        summary_label = "/".join(dict.fromkeys(ordered_labels))
        parts = [f"**{cnt}** {_LABEL_VN[lbl]}" for lbl, cnt in label_counts.items()]
        lines.append(f"Tìm thấy tổng cộng **{total}** {summary_label}, gồm {' và '.join(parts)}.")
    else:
        lines.append(f"Tìm thấy tổng cộng **{total}**  kết quả.")
    lines.append("")

    # Helps LLM filter accurately when user asks about a specific field/domain.
    domain_counts: dict[str, dict[str, int]] = {}  # {domain: {"Major": N, "Specialization": M}}
    for item in items:
        domain = (item.get("properties") or {}).get("domain", "")
        if domain:
            lbl = (item.get("labels") or [""])[0]
            if domain not in domain_counts:
                domain_counts[domain] = {}
            domain_counts[domain][lbl] = domain_counts[domain].get(lbl, 0) + 1

    if domain_counts and len(domain_counts) > 1:
        lines.append("Phân bổ theo lĩnh vực:")
        for domain, lbl_cnts in sorted(domain_counts.items()):
            detail_parts = []
            for lbl_key in ("Major", "Specialization"):
                if lbl_key in lbl_cnts:
                    detail_parts.append(f"{lbl_cnts[lbl_key]} {_LABEL_VN.get(lbl_key, lbl_key)}")
            subtotal = sum(lbl_cnts.values())
            if detail_parts:
                lines.append(f"- {domain}: {', '.join(detail_parts)} ({subtotal} tổng)")
            else:
                lines.append(f"- {domain}: {subtotal}")
        lines.append("")

    if remaining > 0:
        lines.append("Một số kết quả tiêu biểu:")
    else:
        lines.append("Danh sách:")
    lines.append("")

    # Bullet list of display items (always brief: name + description)
    # When mixed Major + Specialization, show hierarchy: Major then indented Specializations
    show_label_prefix = len(label_counts) >= 2
    for item in display_items:
        name = item.get("name", "Unknown")
        lbl = (item.get("labels") or [""])[0]

        # Determine indent and prefix for mixed Major/Specialization
        indent = ""
        if show_label_prefix:
            if lbl == "Specialization":
                item.get("_parent_major")
                indent = "  "  # indent under parent Major
                name = f"Chuyên ngành: {name}"
            elif lbl == "Major":
                name = f"**{name}**"  # bold Major as group header

        description = [
            v for k in DESCRIPTION_ALIAS_KEYS if (v := item.get(k) or item.get("properties", {}).get(k)) is not None
        ]

        # Fallback for Person: use role from relationship when description is empty
        if not description:
            rel_props = item.get("relationship_properties", {})
            role = [rel_props.get(k) for k in DESCRIPTION_ALIAS_KEYS if rel_props.get(k) is not None]
            # if isinstance(role, list):
            #     role = ", ".join(str(r) for r in role if r)
            if role:
                description = role

        if description:
            lines.append(f"{indent}- {name}: {description}")
        else:
            lines.append(f"{indent}- {name}")

    # Remaining count
    if remaining > 0:
        lines.append("")
        lines.append(f"[còn {remaining} kết quả]")

    return "\n".join(lines)


#
# Both functions are thin "join" layers that combine:
#   - A pre-scoped candidate pool  (from list_via_subgraph, already filtered to GDU)
#   - Milvus search results        (matched attrs or relations)
# into a standard list_results dict consumed by format_constraint_list.


def get_resolved_target_topics(
    resolved_attr_defs: list[dict[str, Any]] | None, fallback_topics: list[str]
) -> list[str]:
    node_attr_defs = filter_attribute_definitions(resolved_attr_defs, definition_kind="node")
    if not node_attr_defs:
        logger.info(f"get_resolved_target_topics: no resolved attrs, using fallback='{fallback_topics}'")
        return fallback_topics

    # Use the top-scored definition (list is already sorted by similarity_score desc)
    top_def = node_attr_defs[0]
    label = top_def.get("label", "")

    if label:
        logger.info(
            f"get_resolved_target_topics: resolved to '{label}' "
            f"(attr='{top_def.get('attribute_name', '')}', "
            f"score={top_def.get('similarity_score', 0):.3f})"
        )
        return [label]

    logger.info(f"get_resolved_target_topics: no label in top_def, using fallback='{fallback_topics}'")
    return fallback_topics


def build_candidate_pool(
    source_node_ids: list[str],
    target_topics: list[str],
    context_node_type: str = "",
    time: dict[str, Any] | None = None,
    limit: int = 200,
    display_limit: int = 5,
    start_index: int = 0,
) -> dict[str, Any]:
    """
    Unified candidate-pool builder for both attr and relation branches.

    Simplifies the YAML by replacing:
      - 1b_A_faculty + 1b_A
      - 1b_B_faculty + 1b_B

    Logic stays the same:
      - First try PATH_REGISTRY (shared with what_list) for deterministic traversal.
      - If PATH_REGISTRY path fails unexpectedly, fallback to list_via_subgraph.
      - Faculty + (Major|Specialization) => max_depth = 2 in fallback mode
      - otherwise => max_depth = 5 in fallback mode
    """
    target_topics = target_topics or []
    start_index = start_index or 0
    display_limit = display_limit or 5

    if source_node_ids and context_node_type in target_topics:
        from dbs.neo4j_helper import connect_neo4j

        logger.info(
            "build_candidate_pool: context type '%s' is already a target topic; using identity pool",
            context_node_type,
        )
        driver = connect_neo4j()
        with driver.session() as session:
            result = session.run(
                """
                UNWIND $ids AS sid
                MATCH (n {id: sid})
                RETURN n.id AS id,
                       n.name AS name,
                       n.description AS description,
                       labels(n) AS labels,
                       properties(n) AS properties
                """,
                ids=source_node_ids,
            )
            rows_by_id = {
                rec["id"]: {
                    "name": rec["name"] or "",
                    "id": rec["id"],
                    "description": rec["description"] or "",
                    "labels": list(rec["labels"] or []),
                    "relationship_type": "",
                    "relationship_properties": {},
                    "properties": dict(rec["properties"] or {}),
                }
                for rec in result
            }

        rows = [rows_by_id[node_id] for node_id in source_node_ids if node_id in rows_by_id]
        total = len(rows)
        return {
            "items": rows[start_index : start_index + display_limit],
            "total": total,
            "display_limit": display_limit,
            "start_index": start_index,
            "list_last_index": min(start_index + display_limit, total),
            "intermediate_nodes": [],
        }

    use_faculty_shortcut = context_node_type == "Faculty" and any(
        t in ["Major", "Specialization"] for t in target_topics
    )
    max_depth = 2 if use_faculty_shortcut else 5

    logger.info(
        "build_candidate_pool: context_node_type=%s target_topics=%s max_depth=%d",
        context_node_type,
        target_topics,
        max_depth,
    )

    # Prefer path_registry so what_list and what_constraint_list share
    # the same deterministic pool traversal logic.
    try:
        from services.path_registry import list_via_path_registry

        pool = list_via_path_registry(
            source_node_ids=source_node_ids,
            source_node_type=context_node_type,
            target_topics=target_topics,
            display_limit=display_limit,
            start_index=start_index,
            time=time,
        )
        logger.info(
            "build_candidate_pool: used path_registry -> total=%d",
            (pool or {}).get("total", 0),
        )
        return pool
    except Exception as e:
        logger.error("build_candidate_pool: path_registry failed, fallback to list_via_subgraph: %s", e)

    return list_via_subgraph(
        source_node_ids=source_node_ids,
        target_topics=target_topics,
        max_depth=max_depth,
        limit=limit,
        display_limit=display_limit,
        start_index=start_index,
        time=time,
    )


def candidate_pool_as_constraint_fallback(
    candidate_pool: dict[str, Any] | None = None,
    keywords: list[str] | None = None,
    keyword_attributes: list[str] | None = None,
) -> dict[str, Any] | None:
    candidate_pool_safe = candidate_pool or {}
    pool_items = candidate_pool_safe.get("items", [])
    pool_total = len(pool_items)

    if pool_total == 0:
        logger.info("candidate_pool_as_constraint_fallback: pool empty  None")
        return None

    kw_str = ", ".join(keywords or keyword_attributes or [])
    logger.info(
        f"candidate_pool_as_constraint_fallback: using {pool_total} pool items as fallback (queried keywords: {kw_str})"
    )

    return {
        "items": pool_items,
        "total": pool_total,
        "source": "candidate_pool_fallback",
        "queried_keywords": keyword_attributes or keywords or [],
        "display_limit": candidate_pool_safe.get("display_limit", 5),
        "start_index": candidate_pool_safe.get("start_index", 0),
        "list_last_index": candidate_pool_safe.get("list_last_index", pool_total),
    }


def _iter_major_names(raw_items: Any) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    if not isinstance(raw_items, list):
        return names

    for item in raw_items:
        if isinstance(item, dict):
            values = item.values()
        else:
            values = [item]
        for value in values:
            name = str(value or "").strip()
            marker = name.casefold()
            if not name or marker in seen:
                continue
            seen.add(marker)
            names.append(name)
    return names


def _select_state_major_names(
    interested_majors: list[dict[str, str]] | None = None,
    potential_majors: list[dict[str, str]] | None = None,
) -> tuple[list[str], str | None]:
    interested_names = _iter_major_names(interested_majors)
    if interested_names:
        return interested_names, "interested_majors"

    potential_names = _iter_major_names(potential_majors)
    if potential_names:
        return potential_names, "potential_majors"

    return [], None


def _has_entity_label(entities: list[dict[str, Any]] | None, labels: set[str]) -> bool:
    return any(isinstance(entity, dict) and entity.get("label") in labels for entity in entities or [])


def _is_admission_cutoff_query(
    primary_entities: list[dict[str, Any]] | None = None,
    keyword_attributes: list[str] | None = None,
) -> bool:
    texts: list[str] = []
    for entity in primary_entities or []:
        if not isinstance(entity, dict):
            continue
        texts.append(str(entity.get("label") or ""))
        texts.append(str(entity.get("text") or ""))
    return _is_cutoff_relation_query(primary_texts=texts, keywords=keyword_attributes)


def _is_admission_cutoff_filtered_enumeration(
    original_query: str = "",
    keywords: list[str] | None = None,
) -> bool:
    query_text = " ".join([original_query or "", *(keywords or [])]).casefold()
    if not query_text:
        return False

    op, _threshold, _attr_keys = _parse_numeric_condition(query_text)
    if op is not None:
        return True

    enumeration_markers = (
        "ngành nào",
        "những ngành nào",
        "các ngành nào",
        "chương trình nào",
        "những chương trình nào",
        "các chương trình nào",
    )
    return any(marker in query_text for marker in enumeration_markers)


def _is_school_level_admission_cutoff_query(
    primary_entities: list[dict[str, Any]] | None = None,
    context_entities: list[dict[str, Any]] | None = None,
    keyword_attributes: list[str] | None = None,
    keywords: list[str] | None = None,
    original_query: str = "",
) -> bool:
    return _is_admission_cutoff_query(
        primary_entities=primary_entities,
        keyword_attributes=keyword_attributes,
    ) and _has_entity_label(context_entities, {"University"}) and not _has_entity_label(
        context_entities, {"Major", "Specialization"}
    ) and not _is_admission_cutoff_filtered_enumeration(
        original_query=original_query,
        keywords=keywords,
    )


def build_state_major_candidate_pool_for_admission_score(
    primary_entities: list[dict[str, Any]] | None = None,
    context_entities: list[dict[str, Any]] | None = None,
    keyword_attributes: list[str] | None = None,
    keywords: list[str] | None = None,
    original_query: str = "",
    potential_majors: list[dict[str, str]] | None = None,
    interested_majors: list[dict[str, str]] | None = None,
    time: dict[str, Any] | None = None,
    display_limit: int = 5,
    start_index: int = 0,
) -> dict[str, Any] | None:
    if not _is_school_level_admission_cutoff_query(
        primary_entities=primary_entities,
        context_entities=context_entities,
        keyword_attributes=keyword_attributes,
        keywords=keywords,
        original_query=original_query,
    ):
        return None

    major_names, source = _select_state_major_names(
        interested_majors=interested_majors,
        potential_majors=potential_majors,
    )
    if not major_names:
        return None

    try:
        from dbs.milvus_helper import search_entity_by_name
    except Exception as exc:
        logger.error("build_state_major_candidate_pool_for_admission_score: import failed: %s", exc)
        return _constraint_list_result(
            [],
            "state_major_admission_score",
            display_limit=display_limit,
            start_index=start_index,
            intermediate_nodes=[],
        )

    resolved_by_id: dict[str, dict[str, Any]] = {}
    for name in major_names:
        try:
            results = search_entity_by_name(
                entity_name=[name],
                entity_type=["Major", "Specialization"],
                top_k=1,
                threshold=0.55,
                time=time,
            )
        except Exception as exc:
            logger.error("Failed to resolve state major '%s' for admission score: %s", name, exc)
            continue

        for result in results or []:
            node_id = result.get("node_id") or result.get("id")
            node_type = result.get("node_type") or result.get("type")
            if not node_id or node_type not in {"Major", "Specialization"}:
                continue
            resolved_by_id[str(node_id)] = {
                "name": result.get("node_name") or result.get("name") or name,
                "id": str(node_id),
                "description": result.get("description") or "",
                "labels": [node_type],
                "relationship_type": "",
                "relationship_properties": {},
                "properties": {
                    "id": str(node_id),
                    "name": result.get("node_name") or result.get("name") or name,
                    "description": result.get("description") or "",
                    "node_type": node_type,
                },
            }

    items = list(resolved_by_id.values())
    total = len(items)
    logger.info(
        "build_state_major_candidate_pool_for_admission_score: source=%s input=%d resolved=%d",
        source,
        len(major_names),
        total,
    )
    if total == 0:
        return _constraint_list_result(
            [],
            "state_major_admission_score",
            display_limit=display_limit,
            start_index=start_index,
            intermediate_nodes=[],
        )

    return _constraint_list_result(
        items,
        source or "state_major_admission_score",
        display_limit=display_limit,
        start_index=start_index,
        intermediate_nodes=[],
    )


def build_admission_score_major_clarification(
    primary_entities: list[dict[str, Any]] | None = None,
    context_entities: list[dict[str, Any]] | None = None,
    keyword_attributes: list[str] | None = None,
    keywords: list[str] | None = None,
    original_query: str = "",
    time: dict[str, Any] | None = None,
    display_limit: int = 5,
    start_index: int = 0,
) -> dict[str, Any] | None:
    if not _is_school_level_admission_cutoff_query(
        primary_entities=primary_entities,
        context_entities=context_entities,
        keyword_attributes=keyword_attributes,
        keywords=keywords,
        original_query=original_query,
    ):
        return None

    from dbs.neo4j_helper import connect_neo4j

    def _query_policy_method_records(query_time: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        effective_from, effective_to = _normalize_temporal_params(query_time)
        relation_temporal = build_temporal_where_clause("r", effective_from, effective_to)
        where_parts = [part for part in [relation_temporal] if part]
        where_clause = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

        query = f"""
        MATCH (am:AdmissionMethod)-[r:BELONGS_TO]->(ap:AdmissionPolicy)
        {where_clause}
        RETURN DISTINCT r.id AS relationship_id,
               type(r) AS relationship_type,
               properties(r) AS relationship_properties,
               am.id AS method_id,
               am.name AS method_name,
               am.description AS method_description,
               labels(am) AS method_labels,
               ap.id AS policy_id,
               ap.name AS policy_name
        ORDER BY policy_name, method_name
        """

        try:
            driver = connect_neo4j()
            with driver.session() as session:
                return [dict(record) for record in session.run(query)]
        except Exception as exc:
            logger.error("build_admission_score_major_clarification failed: %s", exc)
            return []

    def _has_cutoff_score(record: dict[str, Any]) -> bool:
        rel_props = dict(record.get("relationship_properties") or {})
        return _has_nonempty_value(rel_props.get("cutoff_score"))

    def _build_items(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        items = []
        for record in records:
            rel_props = _public_rel_properties(record.get("relationship_properties"))
            matched_attributes = {}
            for key in _CUTOFF_REL_ATTR_KEYS:
                if rel_props.get(key) is not None:
                    matched_attributes[key] = rel_props[key]
            items.append(
                {
                    "name": record.get("method_name") or "",
                    "id": record.get("relationship_id") or record.get("method_id") or "",
                    "description": record.get("method_description") or "",
                    "labels": list(record.get("method_labels") or ["AdmissionMethod"]),
                    "properties": rel_props,
                    "relationship_type": record.get("relationship_type") or "BELONGS_TO",
                    "relationship_properties": rel_props,
                    "matched_attributes": matched_attributes,
                    "method_id": record.get("method_id"),
                    "policy_id": record.get("policy_id"),
                    "policy_name": record.get("policy_name"),
                }
            )
        return items

    records = _query_policy_method_records(time)
    cutoff_records = [record for record in records if _has_cutoff_score(record)]
    fallback_time, current_year, fallback_year = _build_previous_year_time(time)

    if cutoff_records:
        items = _build_items(cutoff_records)
        return _constraint_list_result(
            items,
            "admission_score_policy_method_cutoff",
            display_limit=display_limit,
            start_index=start_index,
            current_year=current_year,
        )

    if fallback_time and fallback_year:
        fallback_records = _query_policy_method_records(fallback_time)
        fallback_cutoff_records = [record for record in fallback_records if _has_cutoff_score(record)]
        if fallback_cutoff_records:
            items = _build_items(fallback_cutoff_records)
            return _constraint_list_result(
                items,
                "admission_score_policy_method_temporal_fallback",
                display_limit=display_limit,
                start_index=start_index,
                current_year=current_year,
                fallback_year=fallback_year,
            )

    items = _build_items(records)
    return _constraint_list_result(
        items,
        "admission_score_major_clarification",
        display_limit=display_limit,
        start_index=start_index,
        requires_major_clarification=True,
    )


def get_cutoff_temporal_fallback(
    context_node_ids: list[str],
    context_node_type: str = "Major",
    keyword_attributes: list[str] | None = None,
    time: dict[str, Any] | None = None,
    primary_texts: list[str] | None = None,
    primary_labels: list[str] | None = None,
) -> dict[str, Any] | None:
    """
    Recover prior-year cutoff/quota data when the current-year constraint
    branch has no score data. If a specific primary Major/Specialization is
    present, resolve it and use that node as the historical lookup target even
    when the context scope is broader, such as University.
    """
    # Only run for cutoff-related queries
    kw_set = {k.lower() for k in (keyword_attributes or [])}
    kw_set.update(k.lower() for k in (primary_texts or []) if k)
    if not kw_set & _CUTOFF_OR_QUOTA_SIGNALS:
        logger.info("get_cutoff_temporal_fallback: not a cutoff query  skip")
        return None

    # Compute fallback year = from_year - 1
    from_year = None
    if time and isinstance(time, dict):
        from_year = time.get("from_year")
    if from_year is None:
        logger.info("get_cutoff_temporal_fallback: no from_year in time  skip")
        return None

    try:
        fallback_year = int(from_year) - 1
    except (ValueError, TypeError):
        logger.warning(f"get_cutoff_temporal_fallback: invalid from_year={from_year}")
        return None

    current_primary_entity: dict[str, Any] | None = None

    # Default historical lookup target is the resolved context. Hotfix: when
    # context is University but primary_entities contains the specific Major,
    # resolve and use that Major/Specialization instead.
    target_node_ids = list(context_node_ids or [])
    target_node_type = context_node_type

    normalized_primary_texts = [t for t in (primary_texts or []) if isinstance(t, str) and t.strip()]
    normalized_primary_labels = [label for label in (primary_labels or []) if isinstance(label, str) and label.strip()]

    if normalized_primary_texts:
        try:
            from dbs.milvus_helper import search_entity_by_name

            resolved_primary = search_entity_by_name(
                entity_name=normalized_primary_texts,
                entity_type=normalized_primary_labels or None,
                top_k=1,
                time=time,
            )
            if resolved_primary and isinstance(resolved_primary[0], dict):
                top = resolved_primary[0]
                primary_id = top.get("node_id") or top.get("id")
                primary_type = top.get("node_type") or top.get("type")
                if primary_id and primary_type in ("Major", "Specialization"):
                    current_primary_entity = {
                        "id": primary_id,
                        "name": top.get("node_name") or top.get("name") or normalized_primary_texts[0],
                        "description": top.get("description") or "",
                        "type": primary_type,
                    }
                    target_node_ids = [primary_id]
                    target_node_type = primary_type

        except Exception as e:
            logger.warning(f"get_cutoff_temporal_fallback: resolve primary failed: {e}")

    if not target_node_ids or target_node_type not in ("Major", "Specialization"):
        return None

    # Build temporal params for previous year (only effective_from, no effective_to)
    effective_from = str(fallback_year)

    try:
        from tools.QA.services.neo4j_service import get_neo4j_service

        svc = get_neo4j_service()

        # target_node_ids = target Major/Spec, candidate_labels = AdmissionMethod
        grouped_results = svc._find_shortest_path(
            target_node_ids=target_node_ids,
            candidate_labels=["AdmissionMethod"],
            max_depth=3,
            include_intermediate=True,
            effective_from=effective_from,
            apply_temporal_to_intermediate=False,
            need_to_use_min_hop=True,
        )

        if not grouped_results:
            logger.info("get_cutoff_temporal_fallback: no results from _find_shortest_path")
            return None

        # Extract items with cutoff_score from relationship_details
        items = []
        seen_candidate_ids = set()
        for target_group in grouped_results.values():
            for candidate in target_group.get("candidates", []):
                cid = candidate.get("candidate_id")
                if not cid or cid in seen_candidate_ids:
                    continue
                seen_candidate_ids.add(cid)

                c_name = candidate.get("candidate_name", "") or ""
                rel_details = candidate.get("relationship_details", [])

                # Extract cutoff attrs from relationship properties
                matched_attrs = {}
                for rd in rel_details:
                    props = rd.get("properties", {}) if rd else {}
                    if props.get("cutoff_score") is not None:
                        matched_attrs["cutoff_score"] = props["cutoff_score"]
                    if props.get("estimated_score") is not None:
                        matched_attrs["estimated_score"] = props["estimated_score"]
                    if props.get("quota") is not None:
                        matched_attrs["quota"] = props["quota"]
                    if props.get("effective_from") is not None:
                        matched_attrs["effective_from"] = str(props["effective_from"])[:10]

                # Only include if cutoff_score actually exists
                if "cutoff_score" not in matched_attrs:
                    continue

                items.append(
                    {
                        "name": c_name,
                        "id": cid,
                        "description": "",
                        "matched_attributes": matched_attrs,
                    }
                )

        if not items:
            logger.info("get_cutoff_temporal_fallback: no cutoff_score in relationship data")
            return None

        logger.info(
            f"get_cutoff_temporal_fallback: found {len(items)} cutoff records "
            f"from year {fallback_year} for target {target_node_ids[0][:8]}"
        )
        # logger.info(f"get_cutoff_temporal_fallback: current primary '{current_primary_entity}'")
        if current_primary_entity:
            return {
                "items": [],
                "total": 0,
                "source": "current_description_fallback",
                "fallback_case": "current_description",
                "current_year": str(from_year),
                "current_primary_entity": current_primary_entity,
                "display_limit": 0,
                "start_index": 0,
                "list_last_index": 0,
            }
        else:
            return {
                "items": items,
                "total": len(items),
                "source": "temporal_fallback",
                "fallback_case": "historical_items",
                "fallback_year": str(fallback_year),
                "display_limit": 10,
                "start_index": 0,
                "list_last_index": len(items),
            }

    except Exception as e:
        logger.error(f"get_cutoff_temporal_fallback error: {e}")
        return None


def merge_branch_results(
    attr_filtered_results: dict[str, Any] | None = None,
    rel_filtered_results: dict[str, Any] | None = None,
    keywords: list[str] | None = None,
) -> dict[str, Any] | None:
    a_items = (attr_filtered_results or {}).get("items", [])
    b_items = (rel_filtered_results or {}).get("items", [])
    a_total = len(a_items)
    b_total = len(b_items)

    logger.info(f"merge_branch_results: A={a_total} items, B={b_total} items, keywords={keywords}")

    if a_total > 0 and b_total == 0:
        logger.info("merge_branch_results: only A has results  using A (attribute)")
        return attr_filtered_results
    if b_total > 0 and a_total == 0:
        logger.info("merge_branch_results: only B has results  using B (relation)")
        return rel_filtered_results
    if a_total == 0 and b_total == 0:
        logger.info("merge_branch_results: both empty  returning None")
        return None

    # Instead of hardcoded keyword maps, detect whether each branch
    # carries REAL matched attribute data vs mere entity-search noise.
    #
    # Signal: intersect_pool_with_entities (Branch A entity path) sets
    # This is a search-quality marker, NOT a real domain attribute.
    # Branch B (relation search) carries actual attributes like
    #
    # We score each branch by how many items have REAL attributes
    # (i.e., keys other than "relevance" and "_entity_search_score").

    _NOISE_KEYS = {"relevance", "_entity_search_score"}

    def _count_real_attr_items(items: list) -> int:
        """Count items that carry at least one real domain attribute."""
        count = 0
        for item in items:
            matched = item.get("matched_attributes", {})
            grouped = item.get("matched_attributes_by_method", {})
            # Grouped format always carries real data
            if grouped:
                count += 1
                continue
            # Flat format: check if any key is NOT a noise marker
            real_keys = set(matched.keys()) - _NOISE_KEYS
            if real_keys:
                count += 1
        return count

    a_real = _count_real_attr_items(a_items)
    b_real = _count_real_attr_items(b_items)

    logger.info(
        f"merge_branch_results: A has {a_real}/{a_total} items with real attrs, "
        f"B has {b_real}/{b_total} items with real attrs, keywords={keywords}"
    )

    # Decision logic:
    if b_real > 0 and a_real == 0:
        logger.info("merge_branch_results: B has real attrs, A is noise  using B")
        return rel_filtered_results
    if a_real > 0 and b_real == 0:
        logger.info("merge_branch_results: A has real attrs, B is noise  using A")
        return attr_filtered_results

    keyword_str = " ".join(k.lower() for k in (keywords or []))

    def _keyword_overlap_score(items: list) -> int:
        """Score how well matched_attributes keys overlap with query keywords."""
        score = 0
        for item in items[:5]:  # sample first 5 for efficiency
            matched = item.get("matched_attributes", {})
            grouped = item.get("matched_attributes_by_method", {})
            attr_keys = set(matched.keys()) - _NOISE_KEYS
            if grouped:
                for method_attrs in grouped.values():
                    attr_keys.update(method_attrs.keys())
            for key in attr_keys:
                # Check if attr key (English) or its Vietnamese label appears in keywords
                vn_label = _ATTR_LABEL_MAP.get(key, "")
                key_spaced = key.replace("_", " ")
                if any(term in keyword_str for term in [key, key_spaced, vn_label.lower()] if term):
                    score += 1
        return score

    a_kw_score = _keyword_overlap_score(a_items)
    b_kw_score = _keyword_overlap_score(b_items)

    logger.info(f"merge_branch_results: keyword overlap A={a_kw_score}, B={b_kw_score}")

    if b_kw_score > a_kw_score:
        logger.info("merge_branch_results: B keyword overlap higher  using B")
        return rel_filtered_results
    elif a_kw_score > b_kw_score:
        logger.info("merge_branch_results: A keyword overlap higher  using A")
        return attr_filtered_results
    else:
        # Tie: prefer B (relation data is more specific for constraint queries)
        logger.info("merge_branch_results: tie  preferring B (relation)")
        return rel_filtered_results


def search_entities_in_candidate_pool(
    candidate_pool: dict[str, Any] | None,
    primary_texts: list[str] | None,
    primary_labels: list[str] | None,
    entity_types: list[str] | None,
    keywords: list[str] | None = None,
    keyword_attributes: list[str] | None = None,
    top_k: int = 50,
    threshold: float = 0.45,
    display_limit: int = 5,
    start_index: int = 0,
) -> dict[str, Any] | None:
    """
    Unified Branch A-entity helper:
      search_entity_by_name -> intersect_pool_with_entities

    Replaces YAML pair:
      - A_entity_search
      - A_entity_filter
    """
    if not candidate_pool or not primary_texts or not entity_types:
        logger.info("search_entities_in_candidate_pool: missing pool/texts/types -> None")
        return None

    # Skip semantic entity branch for relation-carrier labels.
    # These belong to Branch B, not semantic Branch A.
    _RELATION_LIKE_LABELS = {
        "AdmissionMethod",
        "AdmissionCombination",
        "AdmissionPolicy",
        "Policy",
    }
    if any(lbl in _RELATION_LIKE_LABELS for lbl in (primary_labels or [])):
        logger.info(
            "search_entities_in_candidate_pool: primary_labels=%s are relation-like -> skip A_entity",
            primary_labels,
        )
        return None

    from dbs.milvus_helper import search_entity_by_name

    entity_search_results = search_entity_by_name(
        entity_name=primary_texts,
        entity_type=entity_types,
        keywords=keywords,
        keyword_attributes=keyword_attributes,
        top_k=top_k,
        threshold=threshold,
    )
    logger.info(
        "search_entities_in_candidate_pool: milvus returned %d entity hits",
        len(entity_search_results or []),
    )
    return intersect_pool_with_entities(
        candidate_pool=candidate_pool,
        entity_search_results=entity_search_results,
        display_limit=display_limit,
        start_index=start_index,
    )


def build_list_from_attr_matches(
    context_node_ids: list[str],
    matched_attr_results: list[dict[str, Any]],
    target_topics: list[str],
    display_limit: int = 5,
    start_index: int = 0,
) -> dict[str, Any]:
    from dbs.neo4j_helper import connect_neo4j

    start_index = start_index or 0
    display_limit = display_limit or 5

    _empty = {
        "items": [],
        "total": 0,
        "source": "attribute",
        "display_limit": display_limit,
        "start_index": start_index,
        "list_last_index": 0,
    }

    if not matched_attr_results or not context_node_ids:
        logger.info("build_list_from_attr_matches: empty inputs, returning empty")
        return _empty

    # Keep only the highest-score match per node (avoids duplicate highlights)
    attr_map: dict[str, dict] = {}
    for r in matched_attr_results:
        nid = r.get("node_id")
        if not nid:
            continue
        score = float(r.get("score", 0.0))
        if nid not in attr_map or score > attr_map[nid]["score"]:
            attr_map[nid] = {
                "attribute_name": r.get("attribute_name", ""),
                "attribute_value": r.get("attribute_value", ""),
                "score": score,
            }

    if not attr_map:
        logger.info("build_list_from_attr_matches: no valid node_ids in matched_attr_results")
        return _empty

    matched_node_ids = list(attr_map.keys())
    logger.info(
        f"build_list_from_attr_matches: {len(matched_node_ids)} candidate nodes "
        f"from Milvus, filtering by {target_topics} reachable from context"
    )

    driver = connect_neo4j()

    query_template = """
    UNWIND $ctx_ids AS cid

    // (a) Direct: matched node is itself a target_label_expr reachable from context
    MATCH p = (ctx {{id: cid}})-[*1..3]-(t:{target_label_expr})
    WHERE NONE(n IN nodes(p)[1..-1]
          WHERE any(lbl IN labels(n) WHERE lbl IN $blocked_intermediate_labels))
    WITH DISTINCT t
    WHERE t.id IN $matched_ids
    RETURN t.id          AS id,
           t.name        AS name,
           t.description AS description,
           labels(t)     AS labels,
           properties(t) AS properties,
           t.id          AS matched_id

    UNION

    UNWIND $ctx_ids AS cid

    // (b) Indirect: matched node is a child of target_label_expr (e.g., AcademicProgram → Major)
    MATCH p = (ctx {{id: cid}})-[*1..3]-(t:{target_label_expr})
    WHERE NONE(n IN nodes(p)[1..-1]
          WHERE any(lbl IN labels(n) WHERE lbl IN $blocked_intermediate_labels))
    WITH DISTINCT t
    MATCH (t)-[*1..2]-(child)
    WHERE child.id IN $matched_ids
      AND child.id <> t.id
    RETURN DISTINCT
        t.id          AS id,
        t.name        AS name,
        t.description AS description,
        labels(t)     AS labels,
        properties(t) AS properties,
        child.id      AS matched_id
    """

    try:
        rows_by_id: dict[str, dict] = {}

        with driver.session() as session:
            target_label_expr = "|".join(target_topics)
            query = query_template.format(target_label_expr=target_label_expr)
            result = session.run(
                query,
                ctx_ids=context_node_ids,
                matched_ids=matched_node_ids,
                blocked_intermediate_labels=list(BLOCKED_INTERMEDIATE_LABELS),
            )
            for r in result:
                tid = r["id"]
                if not tid or tid in rows_by_id:
                    # First occurrence wins (UNION deduplicates by query order: direct > indirect)
                    continue

                matched_id = r["matched_id"]
                attr_info = attr_map.get(matched_id, {})

                raw_props = dict(r["properties"]) if r["properties"] else {}
                keep_keys = {attr_info.get("attribute_name")} | _PERSON_CORE_FIELDS
                clean_props = {k: v for k, v in raw_props.items() if k in keep_keys and v is not None}

                item: dict[str, Any] = {
                    "name": r["name"] or "Unknown",
                    "id": tid,
                    "description": r["description"] or "",
                    # "labels": list(r["labels"]) if r["labels"] else [],
                    "properties": clean_props,
                }

                # Attach the matched attribute so format_constraint_list / LLM
                # can display and filter by value (e.g., "graduation_rate: 95%")
                if attr_info.get("attribute_name"):
                    item["matched_attributes"] = {attr_info["attribute_name"]: attr_info["attribute_value"]}

                rows_by_id[tid] = item

        rows = sorted(rows_by_id.values(), key=lambda x: x.get("name", ""))
        total = len(rows)
        list_last_index = min(start_index + display_limit, total)

        logger.info(
            f"build_list_from_attr_matches: {total} {target_topics} found "
            f"from {len(matched_node_ids)} Milvus candidates "
            f"(ctx={context_node_ids}, display_limit={display_limit})"
        )
        return {
            "items": rows,
            "total": total,
            "source": "attribute",
            "display_limit": display_limit,
            "start_index": start_index,
            "list_last_index": list_last_index,
        }

    except Exception as e:
        logger.error(f"Error in build_list_from_attr_matches: {e}")
        traceback.print_exc()
        return _empty


def build_list_from_rel_matches(
    candidate_pool: dict[str, Any] | None,
    matched_rel_results: list[dict[str, Any]],
    display_limit: int = 5,
    start_index: int = 0,
    time: dict[str, Any] | None = None,
    required_attr_keys: list[str] | None = None,
) -> dict[str, Any]:
    start_index = start_index or 0
    display_limit = display_limit or 5

    _empty = {
        "items": [],
        "total": 0,
        "source": "relation",
        "display_limit": display_limit,
        "start_index": start_index,
        "list_last_index": 0,
    }

    pool_items = (candidate_pool or {}).get("items", [])
    if not matched_rel_results or not pool_items:
        logger.info(
            f"build_list_from_rel_matches: empty inputs (pool={len(pool_items)}, rels={len(matched_rel_results or [])})"
        )
        return _empty

    pool_id_set = {item["id"] for item in pool_items if item.get("id")}
    required_attr_set = set(required_attr_keys or [])

    # For categorical attrs (role, position, title), FTS returns fuzzy matches:
    #
    # against exact-matching DB values.
    #
    # false negatives from score-gap heuristics that can misfire when the best
    _CATEGORICAL_ATTRS_PREFILT = {"role", "status", "title", "position", "chức vụ", "vai trò"}
    if matched_rel_results:
        fts_attrs = {r.get("attribute", "") for r in matched_rel_results if r.get("attribute")}
        is_categorical = bool(fts_attrs) and fts_attrs.issubset(_CATEGORICAL_ATTRS_PREFILT)

        if is_categorical:
            max_score = max(r.get("final_score", 0) for r in matched_rel_results)
            if max_score >= 0.95:
                before_count = len(matched_rel_results)
                matched_rel_results = [r for r in matched_rel_results if r.get("final_score", 0) >= 0.9]
                logger.info(
                    "build_list_from_rel_matches: categorical exact-match filter "
                    "(%s) → %d/%d results kept (max_score=%.3f)",
                    fts_attrs,
                    len(matched_rel_results),
                    before_count,
                    max_score,
                )

    # Structure: rel_map[pool_node_id][source_id][attr_name] = attr_value
    # Grouping by source_node_id prevents collision between different
    rel_map: dict[str, dict[str, dict[str, str]]] = {}
    # Track all non-pool source IDs for name resolution later
    all_source_ids: set = set()

    for r in matched_rel_results:
        src = r.get("source_node_id", "")
        dst = r.get("destination_node_id", "")

        attr_name = r.get("attribute", "")
        attr_value = r.get("attribute_value", "")

        # Determine which endpoint is pool (Major) and which is source (AM)
        pool_node_id = None
        source_id = None
        if src in pool_id_set:
            pool_node_id = src
            source_id = dst
        elif dst in pool_id_set:
            pool_node_id = dst
            source_id = src

        if not pool_node_id or not attr_name or not source_id:
            continue

        if pool_node_id not in rel_map:
            rel_map[pool_node_id] = {}
        if source_id not in rel_map[pool_node_id]:
            rel_map[pool_node_id][source_id] = {}
        # First-wins per (pool, source, attr): keep FTS best match
        if attr_name not in rel_map[pool_node_id][source_id]:
            rel_map[pool_node_id][source_id][attr_name] = attr_value
        all_source_ids.add(source_id)

    if not rel_map:
        logger.info(
            f"build_list_from_rel_matches: no pool nodes found in rel results "
            f"(pool_size={len(pool_id_set)}, rel_count={len(matched_rel_results)}). "
            f"Likely no relations between these Major IDs and constraint attributes."
        )
        return _empty

    # FTS only returns attributes whose text matched the query.
    # but miss cutoff_score:16.0 (value "16.0" doesn't contain "17").
    # Fetch ALL attributes for the same (src, dst, rel) pairs so LLM sees
    # the complete picture (both cutoff_score and estimated_score).
    try:
        from dbs.milvus_helper import fetch_relation_sibling_attributes

        # Collect unique (src, dst, relation_name) from matched results
        seen_pairs = set()
        relation_pairs = []
        for r in matched_rel_results:
            src = r.get("source_node_id", "")
            dst = r.get("destination_node_id", "")
            rel = r.get("relation_name", "")
            pair_key = (src, dst, rel)
            if pair_key not in seen_pairs and src and dst and rel:
                seen_pairs.add(pair_key)
                relation_pairs.append({"source_node_id": src, "destination_node_id": dst, "relation_name": rel})

        if relation_pairs:
            sibling_rows = fetch_relation_sibling_attributes(relation_pairs)

            enriched_count = 0
            for row in sibling_rows:
                src = row.get("source_node_id", "")
                dst = row.get("destination_node_id", "")
                attr_name = row.get("attribute", "")
                attr_value = row.get("attribute_value", "")
                if not attr_name:
                    continue

                pool_node_id = src if src in pool_id_set else (dst if dst in pool_id_set else None)
                source_id = dst if src in pool_id_set else (src if dst in pool_id_set else None)
                if not pool_node_id or pool_node_id not in rel_map:
                    continue
                if not source_id or source_id not in rel_map.get(pool_node_id, {}):
                    continue

                # Add only if not already present for this (pool, source, attr)
                if attr_name not in rel_map[pool_node_id][source_id]:
                    rel_map[pool_node_id][source_id][attr_name] = attr_value
                    enriched_count += 1

            logger.info(
                "build_list_from_rel_matches: sibling enrichment added %d attrs from %d relation pairs",
                enriched_count,
                len(relation_pairs),
            )
    except Exception as e:
        logger.error("build_list_from_rel_matches: sibling enrichment failed: %s", e)

    # FTS only matches rows whose TEXT contains the query value (e.g. "17").
    # Problems:
    # Fix: gap-fill ALL pool nodes to get complete multi-method picture.
    # Step 3 will filter out irrelevant source groups (only dates, no scores).
    #
    # EXCEPTION: Skip gap-fill for CATEGORICAL attrs (role, status, title, position).
    # ALL pool nodes (e.g., all 191 Persons) when only a few match the condition
    _CATEGORICAL_ATTRS = {"role", "status", "title", "position", "chức vụ", "vai trò"}
    try:
        if matched_rel_results:
            from collections import Counter

            rel_name_counts = Counter(r.get("relation_name", "") for r in matched_rel_results if r.get("relation_name"))
            dominant_rel_name = rel_name_counts.most_common(1)[0][0] if rel_name_counts else ""

            fts_attr_names = {r.get("attribute", "") for r in matched_rel_results if r.get("attribute")}

            # filters (role/position), not value-range filters. All matching nodes are
            # already in rel_map from the FTS step.
            is_categorical_only = bool(fts_attr_names) and fts_attr_names.issubset(_CATEGORICAL_ATTRS)

            if dominant_rel_name and not is_categorical_only:
                from dbs.milvus_helper import _gap_fill_by_pool_nodes

                gap_rows = _gap_fill_by_pool_nodes(
                    pool_node_ids=list(pool_id_set),
                    relation_name=dominant_rel_name,
                )

                missing_before = pool_id_set - set(rel_map.keys())
                gap_enriched = 0
                for row in gap_rows:
                    src = row.get("source_node_id", "")
                    dst = row.get("destination_node_id", "")
                    attr_name = row.get("attribute", "")
                    attr_value = row.get("attribute_value", "")
                    if not attr_name:
                        continue

                    pool_node_id = src if src in pool_id_set else (dst if dst in pool_id_set else None)
                    source_id = dst if src in pool_id_set else (src if dst in pool_id_set else None)
                    if not pool_node_id or not source_id:
                        continue

                    if pool_node_id not in rel_map:
                        rel_map[pool_node_id] = {}
                    if source_id not in rel_map[pool_node_id]:
                        rel_map[pool_node_id][source_id] = {}
                    if attr_name not in rel_map[pool_node_id][source_id]:
                        rel_map[pool_node_id][source_id][attr_name] = attr_value
                        gap_enriched += 1
                    all_source_ids.add(source_id)

                logger.info(
                    "build_list_from_rel_matches: gap-fill enriched %d attrs "
                    "for %d pool nodes (%d were missing, rel=%s)",
                    gap_enriched,
                    len(pool_id_set),
                    len(missing_before),
                    dominant_rel_name,
                )
    except Exception as e:
        logger.error("build_list_from_rel_matches: gap-fill failed: %s", e)
        fts_attr_names = set()

    source_name_map: dict[str, str] = {}
    if all_source_ids:
        try:
            from dbs.neo4j_helper import connect_neo4j

            driver = connect_neo4j()
            with driver.session() as session:
                result = session.run(
                    "UNWIND $ids AS sid MATCH (n {id: sid}) RETURN n.id AS id, n.name AS name",
                    ids=list(all_source_ids),
                )
                for rec in result:
                    nid = rec["id"]
                    name = rec["name"] or nid[:8]
                    # Shorten verbose AM names for display
                    if "Học bạ" in name or "học tập THPT" in name:
                        source_name_map[nid] = "Học bạ"
                    elif "Tốt nghiệp THPT" in name or "thi Tốt nghiệp" in name:
                        source_name_map[nid] = "THPT"
                    elif "đánh giá năng lực" in name or "ĐGNL" in name:
                        source_name_map[nid] = "ĐGNL"
                    else:
                        # Generic: use first 30 chars
                        source_name_map[nid] = name[:30]
            logger.info(
                "build_list_from_rel_matches: resolved %d source names: %s",
                len(source_name_map),
                source_name_map,
            )
        except Exception as e:
            logger.error("build_list_from_rel_matches: source name resolution failed: %s", e)

    # Uses check_temporal_overlap (Python companion to build_temporal_where_clause)
    # for in-memory filtering.  Same interval-overlap algorithm, no extra Neo4j
    # round-trip, safe with mixed ISO string formats from Milvus.
    if time:
        effective_from, effective_to = _normalize_temporal_params(time)
        if effective_from or effective_to:
            from tools.QA.services.neo4j_service import check_temporal_overlap

            query_from = str(effective_from) if effective_from is not None else None
            query_to = str(effective_to) if effective_to is not None else None
            filtered_groups = 0
            removed_groups = 0
            for pool_node_id in list(rel_map.keys()):
                source_groups = rel_map[pool_node_id]
                kept: dict[str, dict[str, str]] = {}
                for source_id, attrs in source_groups.items():
                    if check_temporal_overlap(
                        entity_from=str(attrs.get("effective_from", "") or ""),
                        entity_to=str(attrs.get("effective_to", "") or ""),
                        query_from=query_from,
                        query_to=query_to,
                    ):
                        kept[source_id] = attrs
                        filtered_groups += 1
                    else:
                        removed_groups += 1

                if kept:
                    rel_map[pool_node_id] = kept
                else:
                    del rel_map[pool_node_id]

            logger.info(
                "build_list_from_rel_matches: temporal filter from=%s to=%s kept=%d removed=%d",
                query_from,
                query_to,
                filtered_groups,
                removed_groups,
            )

            if not rel_map:
                logger.info("build_list_from_rel_matches: all source groups filtered by temporal")
                return _empty

    # Only keep pool items whose id appears in rel_map.
    # matched_attributes grouped by method name so LLM sees each separately.

    # Determine which attrs are "relevant" for source group filtering.
    # Sibling attr groups: if FTS matched any attr in a group, all attrs in
    # that group are relevant. This prevents including unrelated sources
    # (e.g., fee policies when user asked about scores).
    _ATTR_SIBLING_GROUPS = [
        {"cutoff_score", "estimated_score", "quota"},
        {"fee_information", "cohort"},
    ]
    _RELEVANCE_IGNORE_ATTRS = {"effective_from", "effective_to", "relation_type", "description", "status"}
    try:
        _fts_attrs = set(fts_attr_names) - _RELEVANCE_IGNORE_ATTRS
    except NameError:
        _fts_attrs = set()
    relevant_attrs: set = set(_fts_attrs)
    for group in _ATTR_SIBLING_GROUPS:
        if _fts_attrs & group:  # FTS matched at least 1 attr in this group
            relevant_attrs |= group
    # Fallback: if no FTS attrs known, include all score attrs
    if not relevant_attrs:
        relevant_attrs = {"cutoff_score", "estimated_score", "quota", "fee_information", "cohort"}

    # If FTS matched ONLY categorical attrs (e.g., "role") that are NOT in
    # in rel_map is relevant because the FTS match is type-based, not value-based.
    # This prevents dropping persons when count_has_condition is incorrectly true.
    _ALL_SIBLING_ATTRS = set()
    for group in _ATTR_SIBLING_GROUPS:
        _ALL_SIBLING_ATTRS |= group
    skip_relevance_filter = bool(_fts_attrs) and not (_fts_attrs & _ALL_SIBLING_ATTRS)

    matched_items = []
    for item in pool_items:
        nid = item.get("id")
        if not nid or nid not in rel_map:
            continue

        source_groups = rel_map[nid]  # {source_id: {attr_name: attr_value}}

        # Filter: only keep source groups that have at least 1 relevant attr
        # Skip filter entirely for categorical FTS matches (e.g., role)
        if skip_relevance_filter:
            relevant_groups = source_groups
        else:
            relevant_groups: dict[str, dict[str, str]] = {}
            for source_id, attrs in source_groups.items():
                if required_attr_set and not any(attrs.get(key) is not None for key in required_attr_set):
                    continue
                if any(a in relevant_attrs for a in attrs):
                    relevant_groups[source_id] = attrs
            if not relevant_groups:
                continue

        pool_props = item.get("properties", {})
        core_props = {k: v for k, v in pool_props.items() if k in _PERSON_CORE_FIELDS and v is not None}

        lean_item: dict[str, Any] = {
            "name": item.get("name", "Unknown"),
            "id": nid,
        }
        if core_props:
            lean_item["properties"] = core_props
        if item.get("connected_via"):
            lean_item["connected_via"] = item.get("connected_via")

        # Build matched_attributes grouped by method name.
        if len(relevant_groups) == 1:
            source_id, attrs = next(iter(relevant_groups.items()))
            method_name = source_name_map.get(source_id, source_id[:8])
            matched_attributes = {k: v for k, v in attrs.items() if k}
            if matched_attributes:
                lean_item["matched_attributes"] = matched_attributes
                lean_item["method_name"] = method_name
        else:
            # Multiple sources: nest by method name
            grouped_attrs: dict[str, dict[str, str]] = {}
            for source_id, attrs in relevant_groups.items():
                method_name = source_name_map.get(source_id, source_id[:8])
                filtered = {k: v for k, v in attrs.items() if k}
                if filtered:
                    grouped_attrs[method_name] = filtered
            if grouped_attrs:
                lean_item["matched_attributes_by_method"] = grouped_attrs

        matched_items.append(lean_item)

    # Sort alphabetically for consistent output
    matched_items.sort(key=lambda x: x.get("name", ""))

    total = len(matched_items)
    list_last_index = min(start_index + display_limit, total)

    logger.info(
        f"build_list_from_rel_matches: {total} items matched "
        f"from pool_size={len(pool_id_set)}, "
        f"rel_map_size={len(rel_map)}, "
        f"(display_limit={display_limit}, start_index={start_index})"
    )
    return {
        "items": matched_items,
        "total": total,
        "source": "relation",
        "time": time or {},
        "display_limit": display_limit,
        "start_index": start_index,
        "list_last_index": list_last_index,
    }


def search_relations_in_candidate_pool(
    candidate_pool: dict[str, Any] | None,
    primary_texts: list[str] | None = None,
    keywords: list[str] | None = None,
    original_query: str = "",
    top_k: int = 50,
    similarity_threshold: float = 0.35,
    min_final_score: float = 0.45,
    display_limit: int = 5,
    start_index: int = 0,
    time: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """
    Unified Branch B helper:
      merge_search_queries -> search_relation_full_text -> build_list_from_rel_matches

    Replaces YAML trio:
      - B1
      - B2
      - B3
    """
    pool_items = (candidate_pool or {}).get("items", [])
    # logger.info(f"search_relations_in_candidate_pool: candidate_pool has {pool_items}")
    if not pool_items:
        logger.info("search_relations_in_candidate_pool: empty candidate_pool -> None")
        return None

    if _is_cutoff_relation_query(primary_texts=primary_texts, keywords=keywords):
        required_attr_keys = _required_cutoff_relation_attrs(primary_texts=primary_texts, keywords=keywords)
        cutoff_candidate_pool = _normalize_cutoff_candidate_pool(candidate_pool)
        cutoff_pool_items = (cutoff_candidate_pool or {}).get("items", [])
        matched_rel_results = _fetch_cutoff_relation_rows_from_neo4j(
            cutoff_pool_items,
            required_attr_keys=required_attr_keys,
        )
        logger.info(
            "search_relations_in_candidate_pool: direct cutoff Neo4j path matched_rel_results=%d",
            len(matched_rel_results or []),
        )
        if not matched_rel_results:
            return None
        current_results = build_list_from_rel_matches(
            candidate_pool=cutoff_candidate_pool,
            matched_rel_results=matched_rel_results,
            display_limit=display_limit,
            start_index=start_index,
            time=time,
            required_attr_keys=required_attr_keys,
        )
        current_decision_results, current_numeric_filtered = _filter_relation_results_for_numeric_condition(
            current_results,
            original_query=original_query,
            required_attr_keys=required_attr_keys,
        )
        if (current_decision_results or {}).get("total", 0) > 0:
            return current_decision_results if current_numeric_filtered else current_results

        fallback_time, current_year, fallback_year = _build_previous_year_time(time)
        if fallback_time:
            fallback_results = build_list_from_rel_matches(
                candidate_pool=cutoff_candidate_pool,
                matched_rel_results=matched_rel_results,
                display_limit=display_limit,
                start_index=start_index,
                time=fallback_time,
                required_attr_keys=required_attr_keys,
            )
            fallback_decision_results, fallback_numeric_filtered = _filter_relation_results_for_numeric_condition(
                fallback_results,
                original_query=original_query,
                required_attr_keys=required_attr_keys,
            )
            if (fallback_decision_results or {}).get("total", 0) > 0:
                selected_fallback_results = fallback_decision_results if fallback_numeric_filtered else fallback_results
                selected_fallback_results["current_year"] = current_year
                selected_fallback_results["fallback_year"] = fallback_year
                selected_fallback_results["fallback_case"] = "historical_items"
                return selected_fallback_results

        return current_decision_results if current_numeric_filtered else current_results

    from dbs.milvus_helper import search_relation_full_text

    queries = merge_constraint_search_queries(primary_texts=primary_texts, keyword_attributes=keywords)
    matched_rel_results = search_relation_full_text(
        node_ids=[item["id"] for item in pool_items if item.get("id")],
        queries=queries,
        top_k=top_k,
        similarity_threshold=similarity_threshold,
        min_final_score=min_final_score,
    )
    # logger.info("matched_rel_results: %s", matched_rel_results)
    logger.info(
        "search_relations_in_candidate_pool: queries=%s matched_rel_results=%d",
        queries,
        len(matched_rel_results or []),
    )

    if not matched_rel_results:
        return None

    return build_list_from_rel_matches(
        candidate_pool=candidate_pool,
        matched_rel_results=matched_rel_results,
        display_limit=display_limit,
        start_index=start_index,
        time=time,
    )


def _normalize_cutoff_candidate_pool(candidate_pool: dict[str, Any] | None) -> dict[str, Any]:
    """
    Cutoff scores are stored on AdmissionMethod ↔ Major relationships.

    If the user asks about a Specialization, convert that candidate to its
    parent Major before querying cutoff_score. This keeps specialization
    questions answerable without inventing specialization-level cutoff data.
    """
    candidate_pool_safe = candidate_pool or {}
    pool_items = candidate_pool_safe.get("items", [])
    pool_ids = [item.get("id") for item in pool_items if item.get("id")]
    if not pool_ids:
        return candidate_pool_safe

    from dbs.neo4j_helper import connect_neo4j

    query = """
    UNWIND $ids AS input_id
    MATCH (input {id: input_id})
    WHERE input:Major OR input:Specialization
    OPTIONAL MATCH (parent:Major)-[:INCLUDES]-(input)
    WITH input_id, input, CASE WHEN input:Major THEN input ELSE parent END AS major
    WHERE major IS NOT NULL
    RETURN input_id,
           major.id AS id,
           major.name AS name,
           major.description AS description,
           labels(major) AS labels,
           properties(major) AS properties
    """
    try:
        driver = connect_neo4j()
        with driver.session() as session:
            rows = [dict(record) for record in session.run(query, ids=pool_ids)]
    except Exception as e:
        logger.error("_normalize_cutoff_candidate_pool failed: %s", e)
        return candidate_pool_safe

    major_by_input_id = {
        row["input_id"]: {
            "name": row.get("name") or "",
            "id": row.get("id"),
            "description": row.get("description") or "",
            "labels": list(row.get("labels") or ["Major"]),
            "relationship_type": "",
            "relationship_properties": {},
            "properties": dict(row.get("properties") or {}),
        }
        for row in rows
        if row.get("id")
    }

    normalized_items = []
    seen_major_ids = set()
    for item in pool_items:
        normalized = major_by_input_id.get(item.get("id"))
        if not normalized:
            continue
        major_id = normalized.get("id")
        if not major_id or major_id in seen_major_ids:
            continue
        seen_major_ids.add(major_id)
        normalized_item = dict(normalized)
        if item.get("connected_via"):
            normalized_item["connected_via"] = item.get("connected_via")
        normalized_items.append(normalized_item)

    normalized_pool = dict(candidate_pool_safe)
    normalized_pool["items"] = normalized_items
    normalized_pool["total"] = len(normalized_items)
    normalized_pool["list_last_index"] = min(
        (normalized_pool.get("start_index", 0) or 0) + (normalized_pool.get("display_limit", 5) or 5),
        len(normalized_items),
    )
    return normalized_pool


def _build_previous_year_time(time: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, str | None, str | None]:
    if not isinstance(time, dict):
        return None, None, None
    from_year = time.get("from_year")
    if from_year is None:
        return None, None, None
    try:
        current_year = str(int(from_year))
        fallback_year = str(int(from_year) - 1)
    except (TypeError, ValueError):
        return None, None, None
    return {"from_year": int(fallback_year), "to_year": None}, current_year, fallback_year


def _filter_relation_results_for_numeric_condition(
    results: dict[str, Any] | None,
    original_query: str = "",
    required_attr_keys: list[str] | None = None,
) -> tuple[dict[str, Any], bool]:
    results_safe = dict(results or {})
    op, threshold, auto_attr_keys = _parse_numeric_condition(original_query or "")
    if op is None:
        return results_safe, False

    candidate_attr_keys = auto_attr_keys or required_attr_keys
    filtered_items: list[dict[str, Any]] = []
    for item in results_safe.get("items", []) or []:
        filtered_item = _filter_item_numeric_groups(
            item,
            op,
            threshold,
            candidate_attr_keys=candidate_attr_keys,
        )
        if filtered_item:
            filtered_items.append(filtered_item)

    start_index = results_safe.get("start_index", 0) or 0
    display_limit = results_safe.get("display_limit", 5) or 5
    results_safe["items"] = filtered_items
    results_safe["total"] = len(filtered_items)
    results_safe["list_last_index"] = min(start_index + display_limit, len(filtered_items))
    logger.info(
        "relation numeric pre-filter '%s %s' attr_keys=%s -> %d/%d items",
        op,
        threshold,
        candidate_attr_keys,
        len(filtered_items),
        len((results or {}).get("items", []) or []),
    )
    return results_safe, True


def _is_cutoff_relation_query(
    primary_texts: list[str] | None = None,
    keywords: list[str] | None = None,
) -> bool:
    haystack = " ".join(str(x).lower() for x in (primary_texts or []) + (keywords or []) if x)
    return any(signal in haystack for signal in _CUTOFF_OR_QUOTA_SIGNALS)


def _required_cutoff_relation_attrs(
    primary_texts: list[str] | None = None,
    keywords: list[str] | None = None,
) -> list[str]:
    haystack = " ".join(str(x).lower() for x in (primary_texts or []) + (keywords or []) if x)
    required: list[str] = []
    if any(signal in haystack for signal in _CUTOFF_ATTR_QUERY_SIGNALS):
        required.append("cutoff_score")
    if any(signal in haystack for signal in _QUOTA_ATTR_QUERY_SIGNALS):
        required.append("quota")
    return required or ["cutoff_score"]


def _fetch_cutoff_relation_rows_from_neo4j(
    pool_items: list[dict[str, Any]],
    required_attr_keys: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Deterministic fallback for cutoff constraint-list queries.

    Milvus relation FTS can fail to seed a broad pool query such as
    "Những ngành nào có điểm chuẩn trên 16 điểm?" because the value condition
    ("trên 16") does not appear in relation text. For cutoff_score, query Neo4j
    directly for AdmissionMethod relationships and return rows in the same flat
    shape that build_list_from_rel_matches expects.
    """
    pool_ids = [item.get("id") for item in pool_items if item.get("id")]
    if not pool_ids:
        return []

    from dbs.neo4j_helper import connect_neo4j

    return_attrs = [*_CUTOFF_REL_ATTR_KEYS, "relation_type"]
    required_attrs = [attr for attr in (required_attr_keys or ["cutoff_score"]) if attr in _CUTOFF_REL_ATTR_KEYS]
    if not required_attrs:
        required_attrs = ["cutoff_score"]
    query = """
    UNWIND $pool_ids AS pid
    MATCH (m {id: pid})-[r]-(am:AdmissionMethod)
    WHERE any(required_attr IN $required_attrs WHERE r[required_attr] IS NOT NULL)
    UNWIND $return_attrs AS attr
    WITH r, attr, r[attr] AS attr_value
    WHERE attr_value IS NOT NULL
    RETURN startNode(r).id AS source_node_id,
           endNode(r).id AS destination_node_id,
           type(r) AS relation_name,
           attr AS attribute,
           attr_value AS attribute_value,
           1.0 AS final_score
    """
    try:
        driver = connect_neo4j()
        with driver.session() as session:
            return [
                dict(record)
                for record in session.run(
                    query,
                    pool_ids=pool_ids,
                    return_attrs=return_attrs,
                    required_attrs=required_attrs,
                )
            ]
    except Exception as e:
        logger.error("_fetch_cutoff_relation_rows_from_neo4j failed: %s", e)
        return []


def merge_constraint_search_queries(
    primary_texts: list[str] | None = None,
    keyword_attributes: list[str] | None = None,
) -> list[str]:
    """
    Local query merger for what_constraint_list only.

    We intentionally keep this logic local so tuning constraint-list
    relation search does not change query behavior of other flows that
    still use dbs.graph_search_helpers.merge_search_queries.
    """
    queries: list[str] = []
    seen = set()
    logger.info("merge_constraint_search_queries primary_texts=%s", primary_texts)
    logger.info("merge_constraint_search_queries keyword_attributes=%s", keyword_attributes)

    for source in [keyword_attributes, primary_texts]:
        if not source:
            continue
        items = source if isinstance(source, list) else [source]
        for item in items:
            if not item:
                continue
            normalized = str(item).strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            queries.append(normalized)

    if not queries:
        logger.warning("merge_constraint_search_queries: empty -> ['']")
        return [""]

    merged_query = ", ".join(queries)
    logger.info("merge_constraint_search_queries: %s", merged_query)
    return [merged_query]


# Vietnamese labels for common relation attributes.
# Used in format_constraint_list so LLM can map English attr names to Vietnamese meaning.
_ATTR_LABEL_MAP: dict[str, str] = {
    "cutoff_score": "điểm chuẩn",
    "estimated_score": "điểm dự kiến",
    "quota": "chỉ tiêu",
    "graduation_rate": "tỉ lệ tốt nghiệp",
    "graduate_employment_rate": "tỉ lệ có việc làm sau tốt nghiệp",
    "effective_from": "hiệu lực từ",
    "effective_to": "hiệu lực đến",
    "fee_information": "học phí",
    "cohort": "khóa",
}


def _parse_numeric_condition(query: str):
    q = query.lower()

    op = None
    threshold = None
    cond_start = None  # character position where the condition keyword begins

    # Symbolic operators first (">= 18", "> 16", "<= 20", "< 15")
    m = _re.search(r"(>=|<=|>|<)\s*(\d+(?:\.\d+)?)", q)
    if m:
        op = m.group(1)
        threshold = float(m.group(2))
        cond_start = m.start()
    else:
        patterns = [
            (r"(?:từ|tối thiểu|ít nhất|không dưới|bằng hoặc trên)\s+(\d+(?:\.\d+)?)", ">="),
            (r"(?:trên|hơn|lớn hơn|cao hơn|vượt|quá)\s+(\d+(?:\.\d+)?)", ">"),
            (r"(?:dưới|thấp hơn|nhỏ hơn|ít hơn)\s+(\d+(?:\.\d+)?)", "<"),
            (r"(?:tối đa|không quá|bằng hoặc dưới)\s+(\d+(?:\.\d+)?)", "<="),
        ]
        for pattern, op_val in patterns:
            m = _re.search(pattern, q)
            if m:
                op = op_val
                threshold = float(m.group(1))
                cond_start = m.start()
                break

    if op is None:
        return None, None, []

    prefix = q[:cond_start] if cond_start else q
    best_pos = -1
    best_key = None

    for eng_key, vn_label in _ATTR_LABEL_MAP.items():
        # Try Vietnamese label
        pos = prefix.rfind(vn_label.lower())
        if pos >= 0 and pos > best_pos:
            best_pos = pos
            best_key = eng_key
        # Try English name with spaces (e.g. "cutoff score")
        spaced = eng_key.replace("_", " ")
        pos = prefix.rfind(spaced)
        if pos >= 0 and pos > best_pos:
            best_pos = pos
            best_key = eng_key

    target_attr_keys = [best_key] if best_key else []

    logger.info(f"_parse_numeric_condition: '{query}'  op={op}, threshold={threshold}, auto_attr={target_attr_keys}")
    return op, threshold, target_attr_keys


def _collect_numeric_values(attrs: dict) -> list[float]:
    """
    Extract all numeric values from an attribute dict.
    Skip non-parseable strings (e.g. fee text, date strings).
    """
    nums = []
    for v in attrs.values():
        parsed = _parse_numeric_scalar(v)
        if parsed is not None:
            nums.append(parsed)
    return nums


def _parse_numeric_scalar(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str):
        return None

    s = value.strip()
    if not s:
        return None

    # Percent values like "90%" or "90.5 %"
    m = _re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*%", s)
    if m:
        return float(m.group(1).replace(",", "."))

    # Plain numeric values only; reject strings with extra units/text.
    m = _re.fullmatch(r"\d+(?:[.,]\d+)?", s)
    if m:
        return float(s.replace(",", "."))

    return None


def _method_is_compatible_scale(nums: list[float], threshold: float) -> bool:
    if threshold <= 0 or not nums:
        return True
    scale_ratio = 10.0  # configurable: scale gap must be > 10× to be "different"
    return not all(n > threshold * scale_ratio for n in nums)


def _item_passes_numeric_filter(
    item: dict,
    op: str,
    threshold: float,
    candidate_attr_keys: list[str] | None = None,
) -> bool:
    """
    Return True if this item has at least one admission method that
    satisfies the numeric condition (op, threshold).

    When candidate_attr_keys are provided (already-resolved English attr
    keys, e.g. ["cutoff_score"]), ONLY those fields are checked — no
    fallback to scanning all numeric values.  This prevents
    estimated_score from causing false positives when user asked about
    cutoff_score.

    When candidate_attr_keys is empty/None, falls back to scanning all
    numeric values with scale-compatibility filtering.
    """
    ops = {
        ">": lambda v: v > threshold,
        ">=": lambda v: v >= threshold,
        "<": lambda v: v < threshold,
        "<=": lambda v: v <= threshold,
    }
    check = ops.get(op)
    if not check:
        return True  # unknown op → don't filter out

    def _check_attrs(attrs: dict) -> bool:
        nums = _collect_numeric_values(attrs)
        if not _method_is_compatible_scale(nums, threshold):
            return False  # different scale — skip entirely
        # Precise mode: only check specified attribute keys
        if candidate_attr_keys:
            for key in candidate_attr_keys:
                raw = attrs.get(key)
                val = _parse_numeric_scalar(raw)
                if val is not None and _method_is_compatible_scale([val], threshold) and check(val):
                    return True
            return False
        for val in nums:
            if _method_is_compatible_scale([val], threshold) and check(val):
                return True
        return False

    # Grouped: matched_attributes_by_method = {method: {attr: value}}
    grouped = item.get("matched_attributes_by_method")
    if grouped:
        return any(_check_attrs(attrs) for attrs in grouped.values())

    # Flat: matched_attributes = {attr: value}
    flat = item.get("matched_attributes", {})
    if flat:
        return _check_attrs(flat)

    return False


def _filter_item_numeric_groups(
    item: dict[str, Any],
    op: str,
    threshold: float,
    candidate_attr_keys: list[str] | None = None,
) -> dict[str, Any] | None:
    filtered_item = dict(item)

    grouped = item.get("matched_attributes_by_method")
    if grouped:
        filtered_grouped = {
            method: attrs
            for method, attrs in grouped.items()
            if _item_passes_numeric_filter(
                {"matched_attributes": attrs},
                op,
                threshold,
                candidate_attr_keys=candidate_attr_keys,
            )
        }
        if not filtered_grouped:
            return None
        filtered_item["matched_attributes_by_method"] = filtered_grouped
        return filtered_item

    flat = item.get("matched_attributes")
    if flat:
        if not _item_passes_numeric_filter(
            {"matched_attributes": flat},
            op,
            threshold,
            candidate_attr_keys=candidate_attr_keys,
        ):
            return None
        return filtered_item

    return None


def _detect_constraint_attr_label(items: list[dict[str, Any]], auto_attr_keys: list[str] | None = None) -> str:
    """
    Best-effort label detection for numeric constraint summaries.

    Priority:
      1. auto_attr_keys parsed from query text
      2. first matched attr key found in grouped/flat matched attributes
    """
    for key in auto_attr_keys or []:
        label = _ATTR_LABEL_MAP.get(key)
        if label:
            return label

    for item in items or []:
        grouped = item.get("matched_attributes_by_method") or {}
        for attrs in grouped.values():
            for key in attrs.keys():
                label = _ATTR_LABEL_MAP.get(key)
                if label:
                    return label

        flat = item.get("matched_attributes") or {}
        for key in flat.keys():
            label = _ATTR_LABEL_MAP.get(key)
            if label:
                return label

    return ""


def _format_time_scope(time: dict[str, Any] | None = None) -> str:
    if not isinstance(time, dict):
        return ""
    from_year = time.get("from_year")
    from_month = time.get("from_month")
    to_year = time.get("to_year")
    to_month = time.get("to_month")
    if from_year and from_month and to_year and to_month:
        return f"giai đoạn **{from_year}/{from_month}-{to_year}/{to_month}**"
    if from_year and from_month:
        return f"tháng **{from_month}/{from_year}**"
    if to_year and to_month:
        return f"đến tháng **{to_month}/{to_year}**"
    if from_year and to_year:
        return f"giai đoạn **{from_year}-{to_year}**"
    if from_year:
        return f"năm **{from_year}**"
    if to_year:
        return f"đến năm **{to_year}**"
    return ""


def _format_cutoff_method_attrs(attrs: dict[str, Any]) -> str:
    score = attrs.get("cutoff_score")
    quota = attrs.get("quota")

    parts = []
    if score is not None:
        parts.append(f"{score} điểm")
    if quota is not None:
        parts.append(f"chỉ tiêu {quota}")
    return f"{parts[0]} ({', '.join(parts[1:])})" if len(parts) > 1 else ", ".join(parts)


def format_constraint_list(
    data: dict[str, Any],
    keywords: list[str],
    original_query: str = "",
    time: dict[str, Any] | None = None,
) -> str:
    if not data:
        return "Không tìm thấy thông tin liên quan."

    if data.get("source") in {
        "admission_score_policy_method_cutoff",
        "admission_score_policy_method_temporal_fallback",
    }:
        items = data.get("items", [])
        cutoff_texts = []
        seen_cutoff_texts = set()
        for item in items:
            matched = item.get("matched_attributes") or {}
            props = item.get("relationship_properties") or item.get("properties") or {}
            cutoff_score = matched.get("cutoff_score") or props.get("cutoff_score")
            if not _has_nonempty_value(cutoff_score):
                continue
            cutoff_text = str(cutoff_score).strip()
            marker = cutoff_text.casefold()
            if marker in seen_cutoff_texts:
                continue
            seen_cutoff_texts.add(marker)
            cutoff_texts.append(cutoff_text)

        lines = []
        if original_query:
            lines.append(f"## Câu hỏi: {original_query}")
            lines.append("")

        current_year = data.get("current_year")
        fallback_year = data.get("fallback_year")
        if fallback_year and current_year:
            lines.append(
                f"Chưa có dữ liệu `cutoff_score` năm **{current_year}**; "
                f"dưới đây là dữ liệu năm **{fallback_year}** để tham khảo."
            )
        elif current_year:
            lines.append(f"Dữ liệu điểm chuẩn năm **{current_year}**:")
        else:
            lines.append("Dữ liệu điểm chuẩn:")

        lines.append("")
        if cutoff_texts:
            for cutoff_text in cutoff_texts:
                lines.append(f"- {cutoff_text}")
        else:
            lines.append("- Chưa tìm thấy giá trị `cutoff_score` trong dữ liệu quan hệ.")
        lines.append("")
        lines.append("Nếu bạn cần điểm chuẩn theo ngành/chuyên ngành cụ thể, hãy cho mình biết ngành bạn quan tâm.")
        return "\n".join(lines)

    if data.get("source") == "admission_score_major_clarification":
        items = data.get("items", [])
        lines = []
        if original_query:
            lines.append(f"## Câu hỏi: {original_query}")
            lines.append("")
        lines.append(
            "Mình cần biết bạn đang quan tâm ngành hoặc chuyên ngành nào để tra điểm chuẩn chính xác, "
            "vì điểm chuẩn được lưu theo từng ngành và phương thức xét tuyển."
        )
        if items:
            lines.append("")
            lines.append("Các phương thức xét tuyển hiện có trong dữ liệu:")
            for item in items:
                name = item.get("name")
                if name:
                    lines.append(f"- {name}")
        lines.append("")
        lines.append("Bạn cho mình biết ngành/chuyên ngành bạn quan tâm nhé.")
        return "\n".join(lines)

    # 5 AdmissionMethods but none has cutoff_score (2026 provisional).
    # instead of listing 5 methods as if they answered the question.
    if data.get("source") == "candidate_pool_fallback":
        pool_items = data.get("items", [])
        queried_kw = data.get("queried_keywords", keywords or [])
        # Only show Vietnamese keywords (skip English schema names like "cutoff_score")
        vi_kw = [k for k in queried_kw if not k.isascii()]
        kw_str = vi_kw[0] if vi_kw else (queried_kw[0] if queried_kw else "thông tin được hỏi")
        item_names = [it.get("name", "") for it in pool_items[:5]]
        lines = []
        if original_query:
            lines.append(f"## Câu hỏi: {original_query}")
            lines.append("")
        lines.append(
            f"Ngành/đối tượng được hỏi **có tồn tại** trong hệ thống "
            f"với {len(pool_items)} kết quả liên quan, "
            f"tuy nhiên **chưa có dữ liệu về {kw_str}**."
        )
        lines.append("")
        for n in item_names:
            lines.append(f"- {n}")
        return "\n".join(lines)

    source = data.get("source")
    effective_time = data.get("time") if data.get("fallback_year") else (time or data.get("time"))
    time_scope = _format_time_scope(effective_time)

    # Current-year descriptive fallback. This is intentionally separate from
    # temporal_fallback: when the current primary entity has a useful official
    # description, do not mix it with previous-year cutoff rows.
    if source in {"current_description_fallback", "Current description fallback"}:
        current_year = data.get("current_year")
        current_primary = data.get("current_primary_entity") or {}
        current_name = current_primary.get("name") or "đối tượng đang hỏi"
        current_desc = (current_primary.get("description") or "").strip()
        lines = []
        if original_query:
            lines.append(f"## Câu hỏi: {original_query}")
            lines.append("")

        if current_year:
            lines.append(f"Thông tin hiện tại năm **{current_year}** của **{current_name}**:")
        else:
            lines.append(f"Thông tin hiện tại của **{current_name}**:")

        if current_desc:
            lines.append(f"- Mô tả: {current_desc}")
        else:
            lines.append("- Chưa có mô tả chi tiết trong dữ liệu hiện tại.")

        lines.append("")
        return "\n".join(lines)

    # Historical fallback. Use only the historical rows returned in `items` and
    # do not prepend current entity descriptions, because that is handled by the
    # current_description_fallback case above.
    if source == "temporal_fallback":
        fb_year = data.get("fallback_year", "trước đó")
        fb_items = data.get("items", [])
        lines = []
        if original_query:
            lines.append(f"## Câu hỏi: {original_query}")
            lines.append("")

        lines.append(f"Dưới đây là thông tin năm **{fb_year}** đã được công bố chính thức để tham khảo:")
        lines.append("")
        for item in fb_items:
            name = item.get("name", "Unknown")
            matched = item.get("matched_attributes", {})
            attr_parts = []
            if matched.get("cutoff_score") is not None:
                attr_parts.append(f"điểm chuẩn: {matched['cutoff_score']}")
            if matched.get("quota") is not None:
                attr_parts.append(f"chỉ tiêu: {matched['quota']}")
            attr_str = ", ".join(attr_parts) if attr_parts else "không rõ"
            lines.append(f"- **{name}**: {attr_str}")
        lines.append("")
        return "\n".join(lines)

    items = data.get("items", [])

    # _parse_numeric_condition auto-detects BOTH the condition (op, threshold)
    op, threshold, auto_attr_keys = _parse_numeric_condition(original_query)
    filtered_by_condition = False
    if op is not None:
        filtered_items = []
        for item in items:
            filtered_item = _filter_item_numeric_groups(
                item,
                op,
                threshold,
                candidate_attr_keys=auto_attr_keys or None,
            )
            if filtered_item:
                filtered_items.append(filtered_item)
        logger.info(
            f"format_constraint_list: numeric filter '{op} {threshold}' "
            f"(auto_attr={auto_attr_keys}) → {len(filtered_items)}/{len(items)} items pass"
        )
        items = filtered_items
        filtered_by_condition = True
        data["items"] = items
        data["total"] = len(items)
        data["list_last_index"] = min(
            (data.get("start_index", 0) or 0) + (data.get("display_limit", 5) or 5),
            len(items),
        )

    total = len(items)

    if total == 0:
        if filtered_by_condition and op is not None:
            op_label = {">": "trên", ">=": "từ", "<": "dưới", "<=": "tối đa"}.get(op, op)
            target_attr_label = _detect_constraint_attr_label(data.get("items", []), auto_attr_keys)
            scope_suffix = f" trong {time_scope}" if time_scope else ""
            if target_attr_label:
                return (
                    f"Không tìm thấy ngành nào có {target_attr_label} {op_label} "
                    f"**{threshold:g}**{scope_suffix}."
                )
        constraint_str = ", ".join(keywords) if keywords else "điều kiện đã cho"
        return f"Không tìm thấy kết quả phù hợp với: {constraint_str}"

    # Pagination: slice items like format_list_with_total
    display_limit = data.get("display_limit", 5) or 5
    start_index = data.get("start_index", 0) or 0
    display_items = items[start_index : start_index + display_limit]
    remaining = total - (start_index + len(display_items))

    lines = []
    if original_query:
        lines.append(f"## Câu hỏi: {original_query}")
        lines.append("")

    target_attr_label = _detect_constraint_attr_label(items, auto_attr_keys)
    fallback_year = data.get("fallback_year")
    current_year = data.get("current_year")
    if fallback_year and current_year:
        if filtered_by_condition and op is not None and target_attr_label:
            op_label = {">": "trên", ">=": "từ", "<": "dưới", "<=": "tối đa"}.get(op, op)
            lines.append(
                f"Chưa có ngành nào có {target_attr_label} {op_label} **{threshold:g}** "
                f"trong năm **{current_year}**; dưới đây là dữ liệu năm **{fallback_year}** để tham khảo."
            )
        else:
            fallback_attr_label = target_attr_label or "điểm chuẩn"
            lines.append(
                f"Chưa có dữ liệu {fallback_attr_label} năm **{current_year}**; "
                f"dưới đây là dữ liệu năm **{fallback_year}** để tham khảo."
            )
        lines.append("")

    constraint_str = ", ".join(keywords) if keywords else ""
    if constraint_str:
        if filtered_by_condition and op is not None:
            op_label = {">": "trên", ">=": "từ", "<": "dưới", "<=": "tối đa"}.get(op, op)
            if target_attr_label == "điểm chuẩn":
                scope_suffix = f" trong {time_scope}" if time_scope else ""
                lines.append(
                    f"Có **{total}** ngành có {target_attr_label} {op_label} **{threshold:g}**{scope_suffix}:"
                )
            elif target_attr_label:
                scope_suffix = f" trong {time_scope}" if time_scope else ""
                lines.append(
                    f"Tìm thấy **{total}** kết quả có {target_attr_label} {op_label} **{threshold:g}**{scope_suffix}."
                )
            else:
                lines.append(f"Tìm thấy **{total}** kết quả thỏa điều kiện **{constraint_str}**.")
        else:
            scope_suffix = f" trong {time_scope}" if time_scope else ""
            lines.append(f"Tìm thấy **{total}** kết quả có thông tin liên quan đến **{constraint_str}**{scope_suffix}.")
    else:
        lines.append(f"Tìm thấy **{total}** kết quả:")
    lines.append("")

    if target_attr_label != "điểm chuẩn" and remaining > 0:
        lines.append("Một số kết quả tiêu biểu:")
    elif target_attr_label != "điểm chuẩn":
        lines.append("Danh sách:")
    if target_attr_label != "điểm chuẩn":
        lines.append("")

    for item in display_items:
        name = item.get("name", "Unknown")
        # Show matched_attributes inline so LLM can self-filter by value.
        # Supports two formats:
        #   1) Flat: matched_attributes = {attr: value} + method_name (single source)
        #   2) Grouped: matched_attributes_by_method = {method: {attr: value}} (multi-source)
        grouped = item.get("matched_attributes_by_method")
        flat_matched = item.get("matched_attributes", {})

        # Skip noisy/confusing attrs in display.
        # estimated_score is intentionally excluded: showing it alongside cutoff_score
        # causes LLM to confuse the two when evaluating numeric conditions.
        _DISPLAY_SKIP = {"effective_from", "effective_to", "estimated_score", "relation_type", "status"}
        display_attr_key_set = set(auto_attr_keys or [])

        if grouped:
            # Multi-source: show each method on its own sub-line
            lines.append(f"- **{name}**")
            for method_name, attrs in grouped.items():
                if target_attr_label == "điểm chuẩn":
                    attr_str = _format_cutoff_method_attrs(attrs)
                    if attr_str:
                        lines.append(f" - {method_name}: {attr_str}")
                    continue

                attr_parts = []
                for k, v in attrs.items():
                    if k in _DISPLAY_SKIP:
                        continue
                    if target_attr_label != "điểm chuẩn" and display_attr_key_set and k not in display_attr_key_set:
                        continue
                    label = _ATTR_LABEL_MAP.get(k, k)
                    attr_parts.append(f"{label}: {v}")
                if attr_parts:
                    attr_str = ", ".join(attr_parts)
                    lines.append(f" - {method_name}: {attr_str}")
        elif flat_matched:
            # Single source: inline with optional method prefix
            method_name = item.get("method_name", "")
            if target_attr_label == "điểm chuẩn":
                attr_str = _format_cutoff_method_attrs(flat_matched)
            else:
                attr_parts = []
                for k, v in flat_matched.items():
                    if k in _DISPLAY_SKIP:
                        continue
                    if target_attr_label != "điểm chuẩn" and display_attr_key_set and k not in display_attr_key_set:
                        continue
                    label = _ATTR_LABEL_MAP.get(k, k)
                    attr_parts.append(f"{label}: {v}")
                attr_str = ", ".join(attr_parts)
            if method_name:
                lines.append(f"- **{name}** [{method_name}] ({attr_str})")
            else:
                lines.append(f"- **{name}** ({attr_str})")
        else:
            description = item.get("description") or ""
            props = item.get("properties", {})
            desc_text = description or props.get("description", "")
            if desc_text:
                lines.append(f"- **{name}**: {desc_text[:200]}")
            else:
                lines.append(f"- **{name}**")

    if remaining > 0:
        lines.append("")
        lines.append(f"[còn {remaining} kết quả]")
    # logger.info("line 1111111" + "\n".join(lines))
    return "\n".join(lines)
