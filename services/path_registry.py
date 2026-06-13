"""
Path Registry — (context_label, target_label) → {depth, allowed_rels}.

Reuses _find_shortest_path but post-filters by allowed relationship types
from PATH_REGISTRY instead of BLOCKED_INTERMEDIATE_LABELS.
"""

import traceback
from typing import Any

from dbs.graph_search_helpers import _normalize_temporal_params
from services.what_relation_count_list_constraint import (
    _EXPAND_TYPE_MAP,
    _find_intermediate_nodes,
)
from utils.logging_config import get_logger

logger = get_logger(__name__)

# PATH REGISTRY

PATH_REGISTRY: dict[tuple[str, str], dict] = {
    ("University", "Faculty"): {"depth": 1, "rels": ["MANAGES"]},
    ("University", "Major"): {"depth": 2, "rels": ["MANAGES", "TRAINS"]},
    ("University", "Specialization"): {"depth": 3, "rels": ["MANAGES", "TRAINS", "INCLUDES"]},
    ("University", "Person"): {"depth": 2, "rels": ["WORKS_IN", "MANAGES"]},
    ("University", "Club"): {"depth": 2, "rels": ["MANAGES"]},
    ("University", "Activity"): {"depth": 3, "rels": ["MANAGES", "ORGANIZED_BY"]},
    ("University", "Course"): {"depth": 4, "rels": ["MANAGES", "TRAINS", "INCLUDES", "HAS_COURSE"]},
    ("University", "AcademicProgram"): {"depth": 3, "rels": ["MANAGES", "TRAINS", "INCLUDES"]},
    ("University", "AdmissionPolicy"): {"depth": 2, "rels": ["MANAGES", "APPLIES_TO"]},
    ("University", "AdmissionMethod"): {"depth": 3, "rels": ["MANAGES", "APPLIES_TO", "BELONGS_TO"]},
    ("University", "AdmissionCombination"): {"depth": 3, "rels": ["MANAGES", "TRAINS", "APPLIES_TO"]},
    ("University", "CollaborativePartner"): {"depth": 2, "rels": ["MANAGES", "COLLABS_WITH"]},
    ("University", "Campus"): {"depth": 1, "rels": ["HAS_CAMPUS"]},
    ("University", "Department"): {"depth": 1, "rels": ["MANAGES"]},
    ("University", "Center"): {"depth": 1, "rels": ["MANAGES"]},
    ("University", "Institution"): {"depth": 1, "rels": ["MANAGES"]},
    ("University", "Service"): {"depth": 2, "rels": ["MANAGES"]},
    ("University", "Facility"): {"depth": 2, "rels": ["MANAGES"]},
    ("University", "CareerPosition"): {"depth": 3, "rels": ["MANAGES", "TRAINS", "LEADS_TO"]},
    ("University", "Skill"): {"depth": 4, "rels": ["MANAGES", "TRAINS", "LEADS_TO", "REQUIRES"]},
    ("University", "MarketTrend"): {"depth": 3, "rels": ["MANAGES", "TRAINS", "APPLIES_TO"]},
    ("University", "EducationSystem"): {"depth": 2, "rels": ["MANAGES", "TRAINS", "HAS_EDUCATION_SYSTEM"]},
    ("University", "Policy"): {"depth": 2, "rels": ["MANAGES", "TRAINS", "APPLIES_TO"]},
    ("Faculty", "Major"): {"depth": 1, "rels": ["TRAINS"]},
    ("Faculty", "Specialization"): {"depth": 2, "rels": ["TRAINS", "INCLUDES"]},
    ("Faculty", "Person"): {"depth": 1, "rels": ["WORKS_IN"]},
    ("Faculty", "Club"): {"depth": 1, "rels": ["MANAGES"]},
    ("Faculty", "Activity"): {"depth": 2, "rels": ["MANAGES", "ORGANIZED_BY"]},
    ("Faculty", "CollaborativePartner"): {"depth": 1, "rels": ["COLLABS_WITH"]},
    ("Faculty", "Service"): {"depth": 1, "rels": ["MANAGES"]},
    ("Faculty", "Course"): {"depth": 3, "rels": ["TRAINS", "INCLUDES", "HAS_COURSE"]},
    ("Faculty", "AcademicProgram"): {"depth": 2, "rels": ["TRAINS", "INCLUDES"]},
    ("Faculty", "CareerPosition"): {"depth": 2, "rels": ["TRAINS", "LEADS_TO"]},
    ("Faculty", "Skill"): {"depth": 3, "rels": ["TRAINS", "LEADS_TO", "REQUIRES"]},
    ("Faculty", "MarketTrend"): {"depth": 2, "rels": ["TRAINS", "APPLIES_TO"]},
    ("Major", "Specialization"): {"depth": 1, "rels": ["INCLUDES"]},
    ("Major", "AcademicProgram"): {"depth": 1, "rels": ["INCLUDES"]},
    ("Major", "Course"): {"depth": 3, "rels": ["INCLUDES", "HAS_COURSE"]},
    ("Major", "Faculty"): {"depth": 1, "rels": ["TRAINS"]},
    ("Major", "Person"): {"depth": 2, "rels": ["TRAINS", "WORKS_IN"]},
    ("Major", "AdmissionMethod"): {"depth": 1, "rels": ["APPLIES_TO"]},
    ("Major", "AdmissionCombination"): {"depth": 1, "rels": ["APPLIES_TO"]},
    ("Major", "AdmissionPolicy"): {"depth": 1, "rels": ["APPLIES_TO"]},
    ("Major", "Policy"): {"depth": 1, "rels": ["APPLIES_TO"]},
    ("Major", "CareerPosition"): {"depth": 1, "rels": ["LEADS_TO"]},
    ("Major", "Skill"): {"depth": 2, "rels": ["LEADS_TO", "REQUIRES"]},
    ("Major", "MarketTrend"): {"depth": 1, "rels": ["APPLIES_TO"]},
    ("AcademicProgram", "Course"): {"depth": 1, "rels": ["HAS_COURSE"]},
    ("AcademicProgram", "ProgramObjective"): {"depth": 1, "rels": ["HAS_OBJECTIVE"]},
    ("AcademicProgram", "ProgramLearningOutcome"): {"depth": 1, "rels": ["HAS_PLO"]},
    ("AcademicProgram", "LearnerAssessmentMethod"): {"depth": 1, "rels": ["ASSESSED_BY"]},
    ("AcademicProgram", "TeachingAndLearningMethod"): {"depth": 1, "rels": ["USES_METHOD"]},
    ("Specialization", "Major"): {"depth": 1, "rels": ["INCLUDES"]},
    ("Specialization", "AcademicProgram"): {"depth": 1, "rels": ["INCLUDES"]},
    ("Specialization", "Course"): {"depth": 2, "rels": ["INCLUDES", "HAS_COURSE"]},
    ("Specialization", "CareerPosition"): {"depth": 1, "rels": ["LEADS_TO"]},
    ("Specialization", "Skill"): {"depth": 2, "rels": ["LEADS_TO", "REQUIRES"]},
    ("Specialization", "MarketTrend"): {"depth": 1, "rels": ["APPLIES_TO"]},
    ("Specialization", "Policy"): {"depth": 2, "rels": ["INCLUDES", "APPLIES_TO"]},
    ("Specialization", "AdmissionCombination"): {"depth": 2, "rels": ["INCLUDES", "APPLIES_TO"]},
    ("Specialization", "AdmissionMethod"): {"depth": 2, "rels": ["INCLUDES", "APPLIES_TO"]},
    ("Specialization", "AdmissionPolicy"): {"depth": 2, "rels": ["INCLUDES", "APPLIES_TO"]},
    ("AdmissionPolicy", "AdmissionMethod"): {"depth": 1, "rels": ["BELONGS_TO"]},
    ("AdmissionPolicy", "Major"): {"depth": 1, "rels": ["APPLIES_TO"]},
    ("EducationSystem", "Major"): {"depth": 2, "rels": ["APPLIES_TO"]},
    ("EducationSystem", "Specialization"): {"depth": 3, "rels": ["APPLIES_TO", "INCLUDES"]},
}


def lookup_path_config(context_label: str, target_label: str) -> dict | None:
    """Exact match → reverse match → None."""
    return PATH_REGISTRY.get((context_label, target_label)) or PATH_REGISTRY.get((target_label, context_label))


# Post-filter: whitelist rels (replaces BLOCKED_INTERMEDIATE_LABELS)


def _filter_by_allowed_rels(grouped_results: dict, allowed_rels: list[str] | None) -> dict:
    """Drop candidates whose path contains a rel NOT in allowed_rels."""
    if not allowed_rels:
        return grouped_results

    allowed_set = set(allowed_rels)
    for target_group in grouped_results.values():
        target_group["candidates"] = [
            c
            for c in target_group.get("candidates", [])
            if all(rt in allowed_set for rt in c.get("relationship_types", []))
        ]
    return {tid: tg for tid, tg in grouped_results.items() if tg.get("candidates")}


# (same logic as _query_related_nodes lines 164-214, unavoidable
#  duplication since that code is inline and file is read-only)


def _extract_connected_via_names(intermediate_nodes: list[dict[str, Any]] | None) -> list[str]:
    names: list[str] = []
    for node in intermediate_nodes or []:
        name = str(node.get("node_name") or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def _grouped_to_rows(grouped_results: dict) -> list[dict[str, Any]]:
    rows_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for target_group in grouped_results.values():
        for candidate in target_group.get("candidates", []):
            cid = candidate.get("candidate_id")
            if not cid:
                continue

            c_props = candidate.get("candidate_properties", {})
            properties = dict(c_props)
            if c_props.get("node_type") == "Course":
                properties.pop("effective_from", None)
                properties.pop("effective_to", None)
            name = candidate.get("candidate_name", "") or c_props.get("name", "")
            row_key = (cid, name)

            rel_details = candidate.get("relationship_details", [])
            candidate_groups = []
            for rd in rel_details:
                groups = (rd.get("properties") or {}).get("Group", [])
                candidate_groups.extend([groups] if isinstance(groups, str) else groups)
            connected_via = _extract_connected_via_names(candidate.get("intermediate_nodes", []))

            if row_key in rows_by_key:
                existing = rows_by_key[row_key].get("_all_groups", [])
                rows_by_key[row_key]["_all_groups"] = existing + [g for g in candidate_groups if g not in existing]
                existing_connected_via = rows_by_key[row_key].get("connected_via", [])
                rows_by_key[row_key]["connected_via"] = existing_connected_via + [
                    name for name in connected_via if name not in existing_connected_via
                ]
                continue

            first_rel = rel_details[0] if rel_details else {}

            rows_by_key[row_key] = {
                "name": name,
                "id": cid,
                "description": c_props.get("description", "") or "",
                "labels": [c_props["node_type"]] if c_props.get("node_type") else [],
                "relationship_type": first_rel.get("type", "") if first_rel else "",
                "relationship_properties": dict(first_rel.get("properties", {}))
                if first_rel and first_rel.get("properties")
                else {},
                "properties": properties,
                "connected_via": connected_via,
                "_all_groups": candidate_groups,
            }

            # For Specialization, find parent Major from intermediate_nodes
            if c_props.get("node_type") == "Specialization":
                for node in candidate.get("intermediate_nodes", []):
                    if node.get("node_type") == "Major":
                        rows_by_key[row_key]["_parent_major"] = node.get("node_name", "")
                        rows_by_key[row_key]["_parent_major_id"] = node.get("node_id", "")
                        break

    return sorted(rows_by_key.values(), key=lambda x: x.get("name") or "")


# ═══════════════════════════════════════════════════════════════════
# University shortcut: simple MATCH (b:Label) — no path traversal
# ═══════════════════════════════════════════════════════════════════


def list_all_by_label(
    target_topics: list[str],
    display_limit: int = 5,
    start_index: int = 0,
    time: dict[str, Any] | None = None,
    per_target_depth_override: Any | None = None,
) -> dict[str, Any]:
    """
    List ALL nodes of given labels via simple MATCH — used when context is University.
    Returns DISTINCT name + id, with Major/Specialization expansion and parent grouping.
    """
    import re

    from dbs.neo4j_helper import connect_neo4j
    from tools.QA.services.neo4j_service import build_temporal_where_clause

    if start_index is None:
        start_index = 0

    effective_from, effective_to = _normalize_temporal_params(time)

    # Expand Major ↔ Specialization
    seen, expanded = set(), []
    for t in target_topics:
        for et in _EXPAND_TYPE_MAP.get(t, [t]):
            if et not in seen:
                seen.add(et)
                expanded.append(et)
    target_topics = expanded

    driver = connect_neo4j()
    all_rows: list[dict[str, Any]] = []

    try:
        with driver.session() as session:
            for label in target_topics:
                temporal_clause = "" if label == "Course" else build_temporal_where_clause("b", effective_from, effective_to)
                where_part = f"WHERE {temporal_clause}" if temporal_clause else ""

                # For Specialization, also fetch parent Major
                if label == "Specialization":
                    query = f"""
                    MATCH (b:{label})
                    {where_part}
                    OPTIONAL MATCH (parent:Major)-[:INCLUDES]->(b)
                    RETURN DISTINCT b.name AS name, b.id AS id,
                           labels(b)[0] AS label,
                           properties(b) AS properties,
                           parent.name AS parent_major_name,
                           parent.id AS parent_major_id
                    ORDER BY b.name
                    """
                else:
                    query = f"""
                    MATCH (b:{label})
                    {where_part}
                    RETURN DISTINCT b.name AS name, b.id AS id,
                           labels(b)[0] AS label,
                           properties(b) AS properties
                    ORDER BY b.name
                    """

                params: dict[str, Any] = {}
                if effective_from:
                    params["effective_from"] = effective_from
                if effective_to:
                    params["effective_to"] = effective_to

                result = session.run(query, **params)
                for r in result:
                    props = dict(r["properties"]) if r["properties"] else {}
                    props.pop("embedding", None)
                    if r["label"] == "Course":
                        props.pop("effective_from", None)
                        props.pop("effective_to", None)
                    row: dict[str, Any] = {
                        "name": r["name"] or "",
                        "id": r["id"] or "",
                        "description": props.get("description", ""),
                        "labels": [r["label"]] if r["label"] else [],
                        "properties": props,
                    }
                    if label == "Specialization":
                        if r.get("parent_major_name"):
                            row["_parent_major"] = r["parent_major_name"]
                            row["_parent_major_id"] = r["parent_major_id"]
                    all_rows.append(row)

        # Deduplicate
        seen_ids: set = set()
        deduped = []
        for row in all_rows:
            if row["id"] not in seen_ids:
                seen_ids.add(row["id"])
                deduped.append(row)

        # Sort: Major first, then child Specializations grouped under parent
        _LABEL_ORDER = {"Major": 0, "Specialization": 1}
        _PREFIX_RE = re.compile(r"^(Chuyên ngành|Ngành)\s+", re.IGNORECASE)

        major_names = {}
        for row in deduped:
            if (row.get("labels") or [""])[0] == "Major":
                major_names[row["id"]] = _PREFIX_RE.sub("", row.get("name") or "")

        def _sort_key(x):
            raw_name = x.get("name") or ""
            base_name = _PREFIX_RE.sub("", raw_name)
            lbl = (x.get("labels") or [""])[0]
            if lbl == "Major":
                return base_name, 0, ""
            elif lbl == "Specialization" and x.get("_parent_major_id"):
                parent_base = major_names.get(x["_parent_major_id"], base_name)
                return parent_base, 1, base_name
            return base_name, _LABEL_ORDER.get(lbl, 99), ""

        all_rows = sorted(deduped, key=_sort_key)
        total = len(all_rows)
        logger.critical(f"list_all_by_label: total={total} for labels={target_topics}")

        return {
            "items": all_rows,
            "total": total,
            "display_limit": display_limit,
            "start_index": start_index,
            "list_last_index": min(start_index + display_limit, total),
            "intermediate_nodes": [],
        }

    except Exception as e:
        logger.error(f"Error in list_all_by_label: {e}")
        traceback.print_exc()
        return {
            "items": [],
            "total": 0,
            "display_limit": display_limit,
            "start_index": start_index,
            "list_last_index": 0,
            "intermediate_nodes": [],
        }


# ═══════════════════════════════════════════════════════════════════
# MAIN


def list_via_path_registry(
    source_node_ids: list[str],
    source_node_type: str,
    target_topics: list[str],
    display_limit: int = 5,
    start_index: int = 0,
    time: dict | None = None,
    per_target_depth_override: dict[str, int] | None = None,
) -> dict[str, Any]:
    """
    List entities via PATH_REGISTRY.
    Reuses _find_shortest_path, post-filters by allowed_rels.
    Returns same format as list_via_subgraph.
    """
    try:
        # Flow inputs may omit start_index or pass null; default to first page.
        if start_index is None:
            start_index = 0

        from tools.QA.services.neo4j_service import get_neo4j_service

        effective_from, effective_to = _normalize_temporal_params(time)

        target_topics = [t for t in target_topics if t != source_node_type]
        if not target_topics:
            logger.critical(f"list_via_path_registry: all targets removed (same as source_type={source_node_type})")
            return {
                "items": [],
                "total": 0,
                "display_limit": display_limit,
                "start_index": start_index,
                "list_last_index": 0,
                "intermediate_nodes": [],
            }

        seen, expanded = set(), []
        for t in target_topics:
            for et in _EXPAND_TYPE_MAP.get(t, [t]):
                if et not in seen and et != source_node_type:
                    seen.add(et)
                    expanded.append(et)
        target_topics = expanded

        # _find_shortest_path call with the correct max_depth + allowed_rels.
        svc = get_neo4j_service()
        config_groups: dict[tuple, list[str]] = {}  # (depth, rels_tuple) → [labels]
        fallback_targets: list[str] = []

        depth_override_map = {
            str(k): int(v) for k, v in (per_target_depth_override or {}).items() if isinstance(v, int) and v > 0
        }

        for tl in target_topics:
            cfg = lookup_path_config(source_node_type, tl)
            if cfg:
                depth = depth_override_map.get(tl, cfg["depth"])
                key = (depth, tuple(cfg["rels"]))
                config_groups.setdefault(key, []).append(tl)
            else:
                fallback_targets.append(tl)

        all_rows: list[dict[str, Any]] = []

        for (depth, rels_tuple), labels_group in config_groups.items():
            allowed_rels = list(rels_tuple)
            logger.critical(
                f"list_via_path_registry: {source_node_type}→{labels_group}, depth={depth}, rels={allowed_rels}"
            )

            course_labels = [label for label in labels_group if label == "Course"]
            other_labels = [label for label in labels_group if label != "Course"]
            if course_labels:
                from dbs.course_admin_ops import find_course_paths_by_label_without_course_node_temporal

                grouped = find_course_paths_by_label_without_course_node_temporal(
                    target_node_ids=source_node_ids,
                    max_depth=depth,
                    allowed_rels=allowed_rels,
                    time=time,
                )
                all_rows.extend(_grouped_to_rows(grouped))
            if other_labels:
                grouped = svc._find_shortest_path(
                    target_node_ids=source_node_ids,
                    candidate_labels=other_labels,
                    max_depth=depth,
                    include_intermediate=True,
                    effective_from=effective_from,
                    effective_to=effective_to,
                    apply_temporal_to_intermediate=False,
                    need_to_use_min_hop=False,
                    # allowed_rels=allowed_rels,
                )
                all_rows.extend(_grouped_to_rows(grouped))

        if fallback_targets:
            logger.critical(f"list_via_path_registry: FALLBACK {source_node_type}{fallback_targets}, depth=3")
            course_fallback = [label for label in fallback_targets if label == "Course"]
            other_fallback = [label for label in fallback_targets if label != "Course"]
            if course_fallback:
                from dbs.course_admin_ops import find_course_paths_by_label_without_course_node_temporal

                grouped = find_course_paths_by_label_without_course_node_temporal(
                    target_node_ids=source_node_ids,
                    max_depth=3,
                    time=time,
                )
                all_rows.extend(_grouped_to_rows(grouped))
            if other_fallback:
                grouped = svc._find_shortest_path(
                    target_node_ids=source_node_ids,
                    candidate_labels=other_fallback,
                    max_depth=3,
                    include_intermediate=True,
                    effective_from=effective_from,
                    effective_to=effective_to,
                    apply_temporal_to_intermediate=False,
                    need_to_use_min_hop=False,
                )
                all_rows.extend(_grouped_to_rows(grouped))

        seen_keys: set[tuple[str, str]] = set()
        deduped = []
        for row in all_rows:
            key = (str(row.get("id", "")), str(row.get("name", "")))
            if key not in seen_keys:
                seen_keys.add(key)
                deduped.append(row)

        # Sort: group Specializations under their parent Major.
        # Major first, then its child Specializations, then next Major, etc.
        # Majors without children still appear. Specializations without parent
        # (orphan) sort by their own name at the end.
        import re

        _LABEL_ORDER = {"Major": 0, "Specialization": 1}
        _PREFIX_RE = re.compile(r"^(Chuyên ngành|Ngành)\s+", re.IGNORECASE)

        major_names = {}
        for row in deduped:
            if (row.get("labels") or [""])[0] == "Major":
                base = _PREFIX_RE.sub("", row.get("name") or "")
                major_names[row["id"]] = base

        def _sort_key(x):
            raw_name = x.get("name") or ""
            base_name = _PREFIX_RE.sub("", raw_name)
            label = (x.get("labels") or [""])[0]

            if label == "Major":
                # Major sorts by its own name, order=0
                return (base_name, 0, "")
            elif label == "Specialization" and x.get("_parent_major_id"):
                # Specialization sorts under its parent Major name, order=1
                parent_base = major_names.get(x["_parent_major_id"], base_name)
                return (parent_base, 1, base_name)
            else:
                # Other labels or orphan Specializations
                return (base_name, _LABEL_ORDER.get(label, 99), "")

        all_rows = sorted(deduped, key=_sort_key)

        # Reuse _find_intermediate_nodes from existing code
        intermediate_nodes = _find_intermediate_nodes(source_node_ids, target_topics, all_rows)

        total = len(all_rows)
        logger.critical(f"list_via_path_registry: total={total}")

        return {
            "items": all_rows,
            "total": total,
            "display_limit": display_limit,
            "start_index": start_index,
            "list_last_index": min(start_index + display_limit, total),
            "intermediate_nodes": intermediate_nodes,
        }

    except Exception as e:
        logger.error(f"Error in list_via_path_registry: {e}")
        traceback.print_exc()
        return {
            "items": [],
            "total": 0,
            "display_limit": display_limit,
            "start_index": start_index,
            "list_last_index": 0,
            "intermediate_nodes": [],
        }
