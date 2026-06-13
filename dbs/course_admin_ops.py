from typing import Any

from utils.logging_config import get_logger

logger = get_logger(__name__)


def _normalize_relation_entity_ref(entity: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(entity, dict):
        return None

    normalized = {
        "id": entity.get("id") or entity.get("node_id"),
        "name": entity.get("name") or entity.get("node_name"),
        "type": entity.get("type") or entity.get("node_type"),
        "description": entity.get("description"),
        "score": entity.get("score"),
    }
    return {k: v for k, v in normalized.items() if v is not None}


def _build_where_clause(parts: list[str]) -> str:
    valid = [p for p in parts if p]
    if not valid:
        return ""
    return "WHERE " + " AND ".join(f"({p})" for p in valid)


def resolve_course_membership_depth(
    context_ids: list[str],
    context_label: str,
    time: dict[str, Any] | None = None,
) -> int | None:
    """
    Determine max_depth for course LIST lookup.

    Business rules:
    - Major list: include both direct Major→AcademicProgram→Course and
      via Specialization path, so use depth=3.
    - Faculty list: include all Courses under all Majors (direct + via
      Specialization), so use depth=4.
    - Specialization list: keep Specialization scope (depth=2) and do not
      fall back through parent Major.
    - AcademicProgram list: depth=1.
    """
    if not context_ids or not context_label:
        return None

    return {
        "AcademicProgram": 1,
        "Major": 3,
        "Specialization": 2,
        "Faculty": 4,
    }.get(context_label)


def resolve_course_relation_depth(
    context_ids: list[str],
    context_label: str,
    time: dict[str, Any] | None = None,
) -> int | None:
    """
    Determine initial exact depth for course RELATION lookup.

    Business rules:
    - Major relation: check direct major path first (depth=2), then via
      Specialization.
    - Faculty relation: check Faculty→Major path first (depth=3), then via
      Specialization.
    - Specialization relation: check only direct specialization scope
      (depth=2), never parent Major.
    - AcademicProgram relation: depth=1.
    """
    if not context_ids or not context_label:
        return None

    return {
        "AcademicProgram": 1,
        "Major": 2,
        "Specialization": 2,
        "Faculty": 3,
    }.get(context_label)


def build_course_relation_fallback_depths(
    context_label: str,
    resolved_depth: int | None = None,
) -> list[int]:
    """
    Build additional depths for course RELATION lookup.

    - Major: append depth 3 to include Specialization path after checking 2.
    - Faculty: append depth 4 to include Specialization path after checking 3.
    - Specialization: no fallback; keep only the A3 specialization branch.
    """
    if context_label == "Major":
        return [] if resolved_depth == 3 else [3]

    if context_label == "Faculty":
        return [] if resolved_depth == 4 else [4]

    return []


def build_course_depth_override(course_depth: int | None) -> dict[str, int] | None:
    """
    Build per-target depth override map for list_via_path_registry.
    """
    if isinstance(course_depth, int) and course_depth > 0:
        return {"Course": course_depth}
    return None


def _subgraph_has_candidates(subgraph: dict[str, Any] | None) -> bool:
    if not subgraph or not isinstance(subgraph, dict):
        return False

    for conn_key in ("direct_connections", "indirect_connections"):
        connections = subgraph.get(conn_key, {})
        if not isinstance(connections, dict):
            continue

        for target_group in connections.values():
            if not isinstance(target_group, dict):
                continue
            candidates = target_group.get("candidates", [])
            if isinstance(candidates, list) and len(candidates) > 0:
                return True

    return False


def _merge_relation_subgraphs(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    for conn_key in ("direct_connections", "indirect_connections"):
        base_connections = base.get(conn_key, {})
        extra_connections = extra.get(conn_key, {})

        if not isinstance(base_connections, dict) or not isinstance(extra_connections, dict):
            continue

        for target_id, target_group in extra_connections.items():
            if target_id not in base_connections:
                base_connections[target_id] = {
                    **target_group,
                    "candidates": list(target_group.get("candidates", []) or []),
                }
                continue

            existing_group = base_connections[target_id]
            existing_candidates = list(existing_group.get("candidates", []) or [])
            existing_group["candidates"] = existing_candidates + list(target_group.get("candidates", []) or [])

    return base


def _find_relation_paths_at_exact_depth(
    context_ids: list[str],
    primary_ids: list[str],
    depth: int,
    allowed_rels: list[str] | None = None,
    time: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Find all relation paths at exactly one hop depth.

    Course-membership checks need depth order semantics:
    Major/Faculty direct-program paths are checked first, then specialization
    paths are appended even when a shorter path already exists for the same
    course. Neo4j allShortestPaths cannot do that because it suppresses longer
    valid paths once a shorter path exists between the same endpoints.
    """
    if not context_ids or not primary_ids or not isinstance(depth, int) or depth <= 0:
        return {}

    from dbs.graph_search_helpers import _normalize_temporal_params
    from dbs.neo4j_helper import connect_neo4j
    from tools.QA.services.neo4j_service import build_temporal_where_clause

    effective_from, effective_to = _normalize_temporal_params(time)

    rel_type_pattern = ""
    if allowed_rels:
        rel_type_pattern = ":" + "|".join(allowed_rels)

    where_parts = []
    if effective_from or effective_to:
        rel_temporal = build_temporal_where_clause("path_rel", effective_from, effective_to)
        if rel_temporal:
            where_parts.append(f"ALL(path_rel IN relationships(path) WHERE {rel_temporal})")

    where_clause = _build_where_clause(where_parts)

    query = f"""
    UNWIND $candidate_ids AS candidate_id
    UNWIND $target_ids AS target_id
    MATCH path = (candidate {{id: candidate_id}})-[{rel_type_pattern}*{depth}..{depth}]-(target {{id: target_id}})
    {where_clause}
    WITH
        candidate,
        target,
        candidate_id,
        target_id,
        path,
        candidate.name AS candidate_name,
        properties(candidate) AS candidate_properties,
        target.name AS target_name,
        properties(target) AS target_properties,
        length(path) AS path_length
    RETURN
        candidate_id,
        target_id,
        labels(candidate)[0] AS candidate_label,
        labels(target)[0] AS target_label,
        candidate_name,
        candidate_properties,
        target_name,
        target_properties,
        path_length,
        [node IN nodes(path)[1..-1] | {{
            node_id: node.id,
            node_name: node.name,
            node_type: labels(node)[0]
        }}] AS intermediate_nodes,
        [rel IN relationships(path) | type(rel)] AS relationship_types,
        [rel IN relationships(path) | {{
            type: type(rel),
            properties: properties(rel),
            source_id: startNode(rel).id,
            target_id: endNode(rel).id,
            source_name: startNode(rel).name,
            target_name: endNode(rel).name
        }}] AS relationship_details
    """

    params: dict[str, Any] = {"candidate_ids": primary_ids, "target_ids": context_ids}
    if effective_from:
        params["effective_from"] = effective_from
    if effective_to:
        params["effective_to"] = effective_to

    grouped_results: dict[str, Any] = {}
    driver = connect_neo4j()
    with driver.session() as session:
        result = session.run(query, params)
        for record in result:
            target_id = record["target_id"]
            if target_id not in grouped_results:
                target_props = dict(record["target_properties"])
                target_props["node_type"] = record.get("target_label", "")
                grouped_results[target_id] = {
                    "target_id": target_id,
                    "target_name": record.get("target_name", ""),
                    "target_label": record.get("target_label", ""),
                    "target_description": target_props.get("description", ""),
                    "target_properties": target_props,
                    "candidates": [],
                }

            rel_details = []
            for rd in record.get("relationship_details", []):
                rd_dict = dict(rd)
                props = dict(rd_dict.get("properties", {}))
                props.pop("embedding", None)
                rd_dict["properties"] = props
                rel_details.append(rd_dict)

            candidate_props = dict(record["candidate_properties"])
            candidate_props["node_type"] = record.get("candidate_label", "")

            grouped_results[target_id]["candidates"].append(
                {
                    "candidate_id": record["candidate_id"],
                    "candidate_name": record.get("candidate_name", ""),
                    "candidate_properties": candidate_props,
                    "graph_hop": record["path_length"],
                    "relationship_types": record["relationship_types"],
                    "relationship_details": rel_details,
                    "intermediate_nodes": record["intermediate_nodes"],
                }
            )

    return grouped_results


def find_relation_with_ids_exact_depth(
    context_ids: list[str],
    primary_ids: list[str],
    max_depth: int = 3,
    time: dict[str, Any] | None = None,
    fallback_depths: list[int] | None = None,
) -> dict[str, Any] | None:
    """
    Run ID-based relation search with caller-provided max_depth.
    Does NOT apply PATH_REGISTRY depth override.
    Still applies PATH_REGISTRY allowed relation types when resolvable.

    Always evaluates optional fallback depths in order and merges results.
    This lets course-relation checks include both direct major path and
    specialization path when both exist.
    """
    if not context_ids or not primary_ids:
        logger.warning("find_relation_with_ids_exact_depth: missing context_ids or primary_ids")
        return None

    try:
        allowed_rels = None
        try:
            from dbs.neo4j_helper import connect_neo4j
            from services.path_registry import lookup_path_config

            driver = connect_neo4j()
            with driver.session() as session:
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
                    allowed_rels = config.get("rels")
        except Exception as e:
            logger.debug("find_relation_with_ids_exact_depth: PATH_REGISTRY rel policy skipped: %s", e)

        def _query_at_depth(depth: int) -> dict[str, Any]:
            grouped = _find_relation_paths_at_exact_depth(
                context_ids=context_ids,
                primary_ids=primary_ids,
                depth=depth,
                allowed_rels=allowed_rels,
                time=time,
            )
            return {"direct_connections": {}, "indirect_connections": grouped}

        first_depth = max_depth if isinstance(max_depth, int) and max_depth > 0 else 3
        subgraph = _query_at_depth(first_depth)

        candidate_depths = []
        if isinstance(fallback_depths, list):
            for d in fallback_depths:
                if isinstance(d, int) and d > 0 and d != first_depth and d not in candidate_depths:
                    candidate_depths.append(d)

        for depth in candidate_depths:
            retry_subgraph = _query_at_depth(depth)
            if _subgraph_has_candidates(retry_subgraph):
                logger.info(
                    "find_relation_with_ids_exact_depth: fallback depth hit (%s -> %s)",
                    first_depth,
                    depth,
                )
                subgraph = _merge_relation_subgraphs(subgraph, retry_subgraph)

        return subgraph

    except Exception as e:
        logger.exception("find_relation_with_ids_exact_depth error: %s", e)
        return None


def find_course_paths_by_label_without_course_node_temporal(
    target_node_ids: list[str],
    max_depth: int,
    allowed_rels: list[str] | None = None,
    time: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    List Course candidates without applying temporal filters to Course nodes.

    Course validity is represented by curriculum relationships/program paths,
    not by filtering the Course node itself.
    """
    if not target_node_ids or not isinstance(max_depth, int) or max_depth <= 0:
        return {}

    from dbs.graph_search_helpers import _normalize_temporal_params
    from dbs.neo4j_helper import connect_neo4j
    from tools.QA.services.neo4j_service import build_temporal_where_clause

    effective_from, effective_to = _normalize_temporal_params(time)
    rel_type_pattern = ":" + "|".join(allowed_rels) if allowed_rels else ""

    where_parts = []
    if effective_from or effective_to:
        rel_temporal = build_temporal_where_clause("path_rel", effective_from, effective_to)
        if rel_temporal:
            where_parts.append(f"ALL(path_rel IN relationships(path) WHERE {rel_temporal})")
    where_clause = _build_where_clause(where_parts)

    query = f"""
    UNWIND $target_ids AS target_id
    MATCH (target {{id: target_id}})
    MATCH (candidate:Course)
    MATCH path = allShortestPaths((target)-[{rel_type_pattern}*1..{max_depth}]-(candidate))
    {where_clause}
    WITH
        candidate,
        target,
        candidate.id AS candidate_id,
        target_id,
        path,
        candidate.name AS candidate_name,
        properties(candidate) AS candidate_properties,
        target.name AS target_name,
        properties(target) AS target_properties,
        length(path) AS path_length
    RETURN
        candidate_id,
        target_id,
        labels(candidate)[0] AS candidate_label,
        labels(target)[0] AS target_label,
        candidate_name,
        candidate_properties,
        target_name,
        target_properties,
        path_length,
        reverse([node IN nodes(path)[1..-1] | {{
            node_id: node.id,
            node_name: node.name,
            node_type: labels(node)[0]
        }}]) AS intermediate_nodes,
        reverse([rel IN relationships(path) | type(rel)]) AS relationship_types,
        reverse([rel IN relationships(path) | {{
            type: type(rel),
            properties: properties(rel),
            source_id: startNode(rel).id,
            target_id: endNode(rel).id,
            source_name: startNode(rel).name,
            target_name: endNode(rel).name
        }}]) AS relationship_details
    """

    params: dict[str, Any] = {"target_ids": target_node_ids}
    if effective_from:
        params["effective_from"] = effective_from
    if effective_to:
        params["effective_to"] = effective_to

    grouped_results: dict[str, Any] = {}
    driver = connect_neo4j()
    with driver.session() as session:
        result = session.run(query, params)
        for record in result:
            target_id = record["target_id"]
            if target_id not in grouped_results:
                target_props = dict(record["target_properties"])
                target_props["node_type"] = record.get("target_label", "")
                grouped_results[target_id] = {
                    "target_id": target_id,
                    "target_name": record.get("target_name", ""),
                    "target_label": record.get("target_label", ""),
                    "target_description": target_props.get("description", ""),
                    "target_properties": target_props,
                    "candidates": [],
                }

            rel_details = []
            for rd in record.get("relationship_details", []):
                rd_dict = dict(rd)
                props = dict(rd_dict.get("properties", {}))
                props.pop("embedding", None)
                rd_dict["properties"] = props
                rel_details.append(rd_dict)

            candidate_props = dict(record["candidate_properties"])
            candidate_props["node_type"] = record.get("candidate_label", "")
            grouped_results[target_id]["candidates"].append(
                {
                    "candidate_id": record["candidate_id"],
                    "candidate_name": record.get("candidate_name", ""),
                    "candidate_properties": candidate_props,
                    "graph_hop": record["path_length"],
                    "relationship_types": record["relationship_types"],
                    "relationship_details": rel_details,
                    "intermediate_nodes": record["intermediate_nodes"],
                }
            )

    return grouped_results


def _node_ref(node: dict[str, Any]) -> dict[str, str]:
    return {
        "id": str(node.get("node_id") or ""),
        "name": str(node.get("node_name") or ""),
        "type": str(node.get("node_type") or ""),
    }


def _context_ref(context: dict[str, Any]) -> dict[str, str]:
    return {
        "id": str(context.get("id") or ""),
        "name": str(context.get("name") or ""),
        "type": str(context.get("type") or ""),
    }


def _course_ref(entry: dict[str, Any]) -> dict[str, str]:
    c_props = entry.get("candidate_properties", {})
    return {
        "id": str(entry.get("candidate_id") or c_props.get("id") or ""),
        "name": str(entry.get("candidate_name") or c_props.get("name") or ""),
        "type": str(c_props.get("node_type") or "Course"),
    }


def _dedupe_node_refs(nodes: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    deduped = []
    for node in nodes:
        key = (node.get("type", ""), node.get("name", "") or node.get("id", ""))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(node)
    return deduped


def _node_names(nodes: list[dict[str, str]]) -> list[str]:
    return [node.get("name", "") for node in _dedupe_node_refs(nodes) if node.get("name")]


def _compact_course_membership_attributes(attributes: dict[str, Any]) -> dict[str, Any]:
    from dbs.graph_search_helpers import sanitize_neo4j_types

    noisy_keys = {
        "description",
        "objective",
        "key_points",
        "career_opportunities",
        "mission",
        "effective_from",
        "effective_to",
    }
    return sanitize_neo4j_types({k: v for k, v in attributes.items() if k not in noisy_keys})


def _edge_ref(rel_type: str, from_node: dict[str, str], to_node: dict[str, str]) -> dict[str, Any]:
    return {
        "type": rel_type,
        "from": {"id": from_node.get("id", ""), "name": from_node.get("name", ""), "type": from_node.get("type", "")},
        "to": {"id": to_node.get("id", ""), "name": to_node.get("name", ""), "type": to_node.get("type", "")},
    }


def _format_path(nodes: list[dict[str, str]], edges: list[dict[str, Any]]) -> str:
    if not nodes:
        return ""

    parts = [nodes[0].get("name") or nodes[0].get("type", "")]
    for idx, edge in enumerate(edges):
        if idx + 1 >= len(nodes):
            break
        via = edge.get("via_academic_program")
        via_text = f" qua {via.get('name')}" if isinstance(via, dict) and via.get("name") else ""
        parts.append(f"-[{edge.get('type')}{via_text}]->")
        parts.append(nodes[idx + 1].get("name") or nodes[idx + 1].get("type", ""))
    return " ".join(parts)


def _build_ordered_graph_path(entry: dict[str, Any]) -> dict[str, Any]:
    course = _course_ref(entry)
    target = _context_ref(entry.get("_target_context") or {})
    if not target.get("id") and not target.get("name"):
        return {}

    # Entry path is Course -> ... -> target context. Reverse it for business-readable order.
    intermediates = [_node_ref(n) for n in entry.get("intermediate_nodes", [])]
    nodes = [target, *reversed(intermediates), course]
    rel_types = list(reversed(entry.get("relationship_types", []) or []))
    edges = [
        _edge_ref(rel_type, nodes[idx], nodes[idx + 1])
        for idx, rel_type in enumerate(rel_types)
        if idx + 1 < len(nodes)
    ]

    return {
        "graph_hop": entry.get("graph_hop"),
        "nodes": nodes,
        "edges": edges,
        "display": _format_path(nodes, edges),
    }


def _build_business_path(graph_path: dict[str, Any]) -> dict[str, Any]:
    nodes = graph_path.get("nodes") or []
    edges = graph_path.get("edges") or []
    if not nodes or not edges:
        return {}

    ap_index = next((idx for idx, node in enumerate(nodes) if node.get("type") == "AcademicProgram"), None)
    if ap_index is None or ap_index == 0 or ap_index >= len(nodes) - 1:
        return {
            "graph_hop": graph_path.get("graph_hop"),
            "display": graph_path.get("display", ""),
        }

    academic_program = nodes[ap_index]
    owner_index = ap_index - 1
    course = nodes[-1]

    business_nodes = [node for node in nodes[:ap_index] if node.get("type") != "AcademicProgram"]
    if not business_nodes or business_nodes[-1] != nodes[owner_index]:
        business_nodes.append(nodes[owner_index])
    business_nodes.append(course)

    business_edges = []
    for idx in range(max(owner_index, 0)):
        if idx < len(edges):
            business_edges.append(edges[idx])

    owner = nodes[owner_index]
    business_edges.append(
        {
            **_edge_ref("HAS_COURSE", owner, course),
            "via_academic_program": academic_program,
            "source_relationship_types": [edge.get("type") for edge in edges[owner_index : ap_index + 1]],
        }
    )

    specialization = next((node for node in business_nodes if node.get("type") == "Specialization"), None)

    compact_path = {
        "graph_hop": graph_path.get("graph_hop"),
        "scope": business_nodes[0] if business_nodes else {},
        "course": course,
        "via_academic_program": academic_program,
        "display": _format_path(business_nodes, business_edges),
    }
    if specialization:
        compact_path["specialization"] = specialization
    return compact_path


def _build_course_membership_evidence(entries: list[dict]) -> dict[str, Any]:
    """
    Add explicit curriculum membership evidence for Course relation answers.

    Generic relationship compaction mixes AcademicProgram, Specialization, and
    Major names in connected_via. For course membership checks, downstream
    answers need the actual branches: direct Major program and Specialization
    programs.
    """
    membership_paths = []
    majors: list[dict[str, str]] = []
    direct_majors: list[dict[str, str]] = []
    specialization_parent_majors: list[dict[str, str]] = []
    direct_programs: list[dict[str, str]] = []
    specializations: list[dict[str, str]] = []

    seen_paths: set[tuple] = set()
    for entry in entries:
        c_props = entry.get("candidate_properties", {})
        if c_props.get("node_type") != "Course":
            continue

        intermediates = [_node_ref(n) for n in entry.get("intermediate_nodes", [])]
        academic_program = next((n for n in intermediates if n.get("type") == "AcademicProgram"), None)
        specialization = next((n for n in intermediates if n.get("type") == "Specialization"), None)
        major = next((n for n in intermediates if n.get("type") == "Major"), None)
        faculty = next((n for n in intermediates if n.get("type") == "Faculty"), None)
        target_context = _context_ref(entry.get("_target_context") or {})
        if target_context.get("type") == "Major":
            major = target_context
        if target_context.get("type") == "Faculty":
            faculty = target_context

        if not academic_program and not specialization and not major and not faculty:
            continue

        path_key = (
            entry.get("graph_hop"),
            tuple(entry.get("relationship_types", []) or []),
            academic_program.get("name", "") if academic_program else "",
            specialization.get("name", "") if specialization else "",
            major.get("name", "") if major else "",
            faculty.get("name", "") if faculty else "",
        )
        if path_key in seen_paths:
            continue
        seen_paths.add(path_key)

        if specialization:
            path_type = "via_specialization"
            specializations.append(specialization)
            if major:
                majors.append(major)
                specialization_parent_majors.append(major)
        else:
            path_type = "direct_major" if major else "direct_academic_program"
            if major:
                majors.append(major)
                direct_majors.append(major)
            if academic_program:
                direct_programs.append(academic_program)

        graph_path = _build_ordered_graph_path(entry)
        business_path = _build_business_path(graph_path)
        if business_path:
            membership_paths.append({**business_path, "path_type": path_type})

    if not membership_paths:
        return {}

    membership_paths = sorted(membership_paths, key=lambda p: (p.get("graph_hop", 999), p.get("path_type", "")))
    return {
        "membership_summary": {
            "has_direct_course_path": bool(direct_programs),
            "has_specialization_course_path": bool(specializations),
            "majors": _node_names(majors),
            "direct_majors": _node_names(direct_majors),
            "specialization_parent_majors": _node_names(specialization_parent_majors),
            "specializations": _node_names(specializations),
        },
        "membership_paths": membership_paths,
    }


def build_course_membership_candidate_extension(
    entries: list[dict],
    attributes: dict[str, Any],
) -> dict[str, Any]:
    membership_evidence = _build_course_membership_evidence(entries)
    if not membership_evidence:
        return {}

    return {
        "attributes": _compact_course_membership_attributes(attributes),
        "candidate_fields": membership_evidence,
    }


def _format_limited_names(names: list[str], *, limit: int = 5, extra_label: str = "mục khác") -> str:
    if len(names) <= limit:
        return ", ".join(names)
    visible = ", ".join(names[:limit])
    return f"{visible}, và {len(names) - limit} {extra_label}"


def build_course_membership_relation_answer(
    relation_context: dict[str, Any],
    subject: dict[str, Any] | None,
    obj: dict[str, Any] | None,
) -> dict[str, Any] | None:
    candidates = relation_context.get("candidates", []) or []
    if not candidates:
        return None

    course_candidate = next(
        (
            candidate
            for candidate in candidates
            if candidate.get("type") == "Course" and candidate.get("membership_paths")
        ),
        None,
    )
    subject = _normalize_relation_entity_ref(subject)
    obj = _normalize_relation_entity_ref(obj)
    if not course_candidate or not subject or not obj:
        return None

    subject_name = subject.get("name") or "đối tượng được hỏi"
    object_name = obj.get("name") or course_candidate.get("name") or "học phần được hỏi"

    summary = course_candidate.get("membership_summary") or {}
    direct_majors = summary.get("direct_majors") or []
    specializations = summary.get("specializations") or []
    lines = [f"Có. **{object_name}** có trong **{subject_name}**."]

    if subject.get("type") == "Faculty" and direct_majors:
        lines.append(
            f"Các ngành thuộc khoa này có học phần này: "
            f"{_format_limited_names(direct_majors, extra_label='ngành khác')}."
        )

    if subject.get("type") != "Specialization" and specializations:
        specialization_scope = "scope này"
        if subject.get("type") == "Major":
            specialization_scope = "ngành này"
        elif subject.get("type") == "Faculty":
            specialization_scope = "khoa này"
        lines.append(
            f"Các chuyên ngành thuộc {specialization_scope} cũng có học phần này: "
            f"{_format_limited_names(specializations, extra_label='chuyên ngành khác')}."
        )

    paths = course_candidate.get("membership_paths") or []
    relation_types = []
    seen_relation_types = set()
    for path in paths:
        path_type = path.get("path_type")
        if path_type and path_type not in seen_relation_types:
            seen_relation_types.add(path_type)
            relation_types.append(path_type)

    return {
        "formatted_answer": "\n".join(lines),
        "relations": relation_types,
        "course_membership": {
            "scope_entity": subject,
            "course": obj,
            "membership_summary": summary,
            "membership_paths": paths,
        },
    }
