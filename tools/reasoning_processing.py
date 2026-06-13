"""
Reasoning Processing Module.

Provides functions for enriching data through:
1. Sibling node discovery in Neo4j graph
2. Semantic attribute enrichment via Milvus vector search (for nodes)
3. Relation attribute enrichment (semantic + sibling + triangle discovery)

This module is designed for use in reasoning pipelines where additional
context from related nodes, semantically similar attributes, or
relationship metadata is needed.
"""

import json
import re
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any, TypedDict, cast

from google.adk.tools import ToolContext

# Usage of internal modules (Adapted for admission-agent)
from pymilvus import Collection

from dbs.graph_search_helpers import sanitize_neo4j_types
from dbs.milvus_helper import connect_milvus, get_embedding
from tools.QA.services.neo4j_service import Neo4jService, check_temporal_overlap
from utils.logging_config import get_logger

# Logger Setup

logger = get_logger("reasoning.processing")

# Type Aliases

NodeId = str
RelationId = str
AttributeKey = str
AttributeValue = Any
TemporalValue = str | date | datetime | None
NodeAttributeKeysMap = dict[NodeId, list[AttributeKey]]
NodeAttributeValuesMap = dict[NodeId, dict[AttributeKey, AttributeValue]]
ScoredCandidate = tuple[AttributeKey, AttributeValue, float]
RelationAttributeSearchKey = tuple[str, str, tuple[AttributeKey, ...]]


class NodeReasoningItem(TypedDict, total=False):
    req_attrs: list[AttributeKey]
    effective_from: TemporalValue
    effective_to: TemporalValue


NodeReasoningInput = dict[NodeId, list[AttributeKey] | NodeReasoningItem]

# Configuration Constants

# Milvus collection for property key embeddings
MILVUS_PROPERTY_COLLECTION = "knowledge_university_entity_property"

# Enrichment search parameters
ENRICH_TOP_K = 5
ENRICH_SEARCH_LIMIT = 100
ENRICH_SIMILARITY_THRESHOLD = 0.7
ENRICH_MAX_INPUT_ITEMS = 5
ENRICH_MAX_ATTRIBUTES_PER_ITEM = 5
ENRICH_MAX_CANDIDATES_PER_ITEM = 5

# HNSW search parameter for recall quality
HNSW_EF_SEARCH = 200

# Cypher Queries

SIBLING_SEARCH_QUERY = """
UNWIND $batch AS item
MATCH (n {id: item.id})
WITH item, n, labels(n) AS n_labels, item.req_attrs AS req_attrs

// -----------------------------------------------------------------------------
// 1. AcademicProgram Logic
// -----------------------------------------------------------------------------

// Path 1a: Program <- Major -> Program (Local)
OPTIONAL MATCH (n)<-[:INCLUDES]-(parent:Major)-[:INCLUDES]->(s1a:AcademicProgram)
WHERE n <> s1a AND 'AcademicProgram' IN n_labels

// Path 1b: Program <- Specialization -> Program (Local)
OPTIONAL MATCH (n)<-[:INCLUDES]-(parent_s:Specialization)-[:INCLUDES]->(s1b:AcademicProgram)
WHERE n <> s1b AND 'AcademicProgram' IN n_labels

// Path 1c: Program <- Major <- Faculty -> Major -> Program (Remote)
OPTIONAL MATCH (n)<-[:INCLUDES]-(major:Major)<-[:TRAINS]-(faculty:Faculty)
               -[:TRAINS]->(other_major:Major)-[:INCLUDES]->(s1c:AcademicProgram)
WHERE n <> s1c AND 'AcademicProgram' IN n_labels

// Path 1d: Program <- Specialization <- Major <- Faculty -> ... (Remote via Spec)
OPTIONAL MATCH (n)<-[:INCLUDES]-(spec:Specialization)<-[:INCLUDES]-(major2:Major)<-[:TRAINS]-(faculty2:Faculty)
WITH item, n, n_labels, req_attrs, s1a, s1b, s1c, faculty2

// Path 1d (cont): ...Faculty -> Major -> Program
OPTIONAL MATCH (faculty2)-[:TRAINS]->(any_major:Major)-[:INCLUDES]->(s1d:AcademicProgram)
WHERE n <> s1d AND 'AcademicProgram' IN n_labels AND faculty2 IS NOT NULL

// Path 1e: ...Faculty -> Major -> Spec -> Program
OPTIONAL MATCH (faculty2)-[:TRAINS]->(any_major2:Major)-[:INCLUDES]->(any_spec:Specialization)-[:INCLUDES]->(s1e:AcademicProgram)
WHERE n <> s1e AND 'AcademicProgram' IN n_labels AND faculty2 IS NOT NULL

// -----------------------------------------------------------------------------
// 2. Major Logic (Shared Faculty)
// -----------------------------------------------------------------------------
OPTIONAL MATCH (n)<-[:TRAINS]-(faculty_m:Faculty)-[:TRAINS]->(s_major:Major)
WHERE n <> s_major AND 'Major' IN n_labels

// -----------------------------------------------------------------------------
// 3. Specialization Logic
// -----------------------------------------------------------------------------

// Path 3a: Specialization <- Major -> Specialization (Local)
OPTIONAL MATCH (n)<-[:INCLUDES]-(m_parent:Major)-[:INCLUDES]->(s3a:Specialization)
WHERE n <> s3a AND 'Specialization' IN n_labels

// Path 3b: Specialization <- Major <- Faculty -> Major -> Specialization (Remote)
OPTIONAL MATCH (n)<-[:INCLUDES]-(m_spec:Major)<-[:TRAINS]-(f_spec:Faculty)
               -[:TRAINS]->(om_spec:Major)-[:INCLUDES]->(s3b:Specialization)
WHERE n <> s3b AND 'Specialization' IN n_labels

// -----------------------------------------------------------------------------
// 4. Generic Logic (Direct Parent/Child) for other types
// -----------------------------------------------------------------------------
// Generic Fallback: Shared Parent
OPTIONAL MATCH (n)<-[]-(gp)-[]->(s_generic_p)
WHERE n <> s_generic_p 
  AND labels(s_generic_p) = n_labels
  AND (labels(n) = labels(s_generic_p))
  AND NONE(l IN n_labels WHERE l IN ['AcademicProgram', 'Major', 'Specialization'])
  AND NOT gp:EducationSystem AND NOT gp:University AND NOT gp:Campus

// Generic Fallback: Shared Child
OPTIONAL MATCH (n)-[]->(gc)<-[]-(s_generic_c)
WHERE n <> s_generic_c 
  AND labels(s_generic_c) = n_labels
  AND (labels(n) = labels(s_generic_c))
  AND NONE(l IN n_labels WHERE l IN ['AcademicProgram', 'Major', 'Specialization'])
  AND NOT gc:EducationSystem AND NOT gc:University AND NOT gc:Campus

// -----------------------------------------------------------------------------
// Combine Results
// -----------------------------------------------------------------------------
WITH item, n_labels, req_attrs,
     collect(DISTINCT s1a) + collect(DISTINCT s1b) + collect(DISTINCT s1c) + collect(DISTINCT s1d) + collect(DISTINCT s1e) + 
     collect(DISTINCT s_major) + collect(DISTINCT s3a) + collect(DISTINCT s3b) +
     collect(DISTINCT s_generic_p) + collect(DISTINCT s_generic_c) AS candidates
UNWIND candidates AS sibling

// Filter out nulls and ensure same node type
WITH item, sibling, n_labels, req_attrs
WHERE sibling IS NOT NULL AND labels(sibling) = n_labels

// Check if sibling has AT LEAST ONE of the required attributes
WITH item, sibling, req_attrs
WHERE any(attr IN req_attrs WHERE sibling[attr] IS NOT NULL)

// Only return required attributes (not all properties)
WITH item, sibling, req_attrs
UNWIND req_attrs AS attr
WITH item, sibling, attr, sibling[attr] AS value
WHERE value IS NOT NULL
WITH item, sibling, collect([attr, value]) AS pairs
RETURN DISTINCT item.id AS source_node_id,
       apoc.map.merge(apoc.map.fromPairs(pairs), {name: sibling.name}) AS props,
       sibling.effective_from AS sibling_effective_from,
       sibling.effective_to AS sibling_effective_to
LIMIT 5
"""

ATTRIBUTE_VALIDATION_QUERY = """
UNWIND $batch AS item
MATCH (n {id: item.id})
WITH n, item.candidates AS candidates
UNWIND candidates AS key

// Check if property exists and is not null
WITH n, key
WHERE n[key] IS NOT NULL

RETURN n.id AS node_id, key, n[key] AS value
LIMIT 5
"""

FETCH_ATTRIBUTES_QUERY = """
UNWIND $batch AS item
MATCH (n {id: item.id})
WITH n, item.attrs AS attrs
UNWIND attrs AS attr
WITH n, attr
WHERE n[attr] IS NOT NULL
RETURN n.id AS node_id, attr, n[attr] AS value
LIMIT 5
"""

FETCH_NODE_LABELS_QUERY = """
MATCH (n)
WHERE n.id IN $node_ids
RETURN n.id AS node_id, labels(n) AS labels
LIMIT 5
"""


# Custom Wrappers (Adapting to admission-agent architecture)


class Neo4jUtils:
    """Context manager wrapper for Neo4jService to match original logic."""

    def __enter__(self):
        self.service = Neo4jService()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

    def run_query(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Run Cypher query and return list of results."""
        result = self.service.cypher_query(query, params or {})
        if result.get("status") == "success":
            return result.get("results", [])
        else:
            error_msg = result.get("message", "Unknown Neo4j error")
            logger.error(f"Neo4j Query Error: {error_msg}")
            raise Exception(f"Neo4j Error: {error_msg}")


# Helper Functions (Private)


def _fetch_node_labels(node_ids: list[NodeId]) -> dict[NodeId, frozenset]:
    """
    Fetch labels for a list of node IDs.

    Args:
        node_ids: List of node IDs.

    Returns:
        Mapping of NodeId -> FrozenSet of labels.
    """
    if not node_ids:
        return {}

    node_labels: dict[NodeId, frozenset] = {}

    try:
        with Neo4jUtils() as neo4j:
            logger.info(f"Fetching labels for {len(node_ids)} nodes.")
            records = neo4j.run_query(FETCH_NODE_LABELS_QUERY, {"node_ids": list(node_ids)})
            for record in records:
                node_labels[record["node_id"]] = frozenset(record["labels"])
    except Exception as e:
        logger.error(f"Error fetching node labels: {e}", exc_info=True)
        # Fallback implies generic processing or error handling upstream
        raise

    return node_labels


def _collect_unique_keys(input_data: NodeAttributeKeysMap) -> list[AttributeKey]:
    """
    Extract all unique attribute keys from input data.

    Args:
        input_data: Mapping of node IDs to their attributes.

    Returns:
        List of unique attribute keys.
    """
    unique_keys: set[AttributeKey] = set()
    for attrs in input_data.values():
        unique_keys.update(attrs)
    return list(unique_keys)


def _normalize_req_attrs(raw_attrs: Any) -> list[AttributeKey]:
    """Normalize requested attribute keys to a deduplicated list of non-empty strings."""
    if raw_attrs is None:
        return []

    if isinstance(raw_attrs, str):
        raw_values: Sequence[Any] = [raw_attrs]
    elif isinstance(raw_attrs, Sequence):
        raw_values = raw_attrs
    elif isinstance(raw_attrs, set):
        raw_values = list(raw_attrs)
    else:
        logger.warning("Ignoring invalid req_attrs payload of type %s", type(raw_attrs).__name__)
        return []

    normalized_attrs: list[AttributeKey] = []
    seen: set[AttributeKey] = set()

    for raw_attr in raw_values:
        if not isinstance(raw_attr, str):
            logger.warning("Ignoring non-string attribute key of type %s", type(raw_attr).__name__)
            continue

        attr = raw_attr.strip()
        if not attr or attr in seen:
            continue

        seen.add(attr)
        normalized_attrs.append(attr)

    if len(normalized_attrs) > ENRICH_MAX_ATTRIBUTES_PER_ITEM:
        logger.warning(
            "Truncating req_attrs from %s to %s items to keep enrichment bounded.",
            len(normalized_attrs),
            ENRICH_MAX_ATTRIBUTES_PER_ITEM,
        )
        return normalized_attrs[:ENRICH_MAX_ATTRIBUTES_PER_ITEM]

    return normalized_attrs


def _limit_mapping_items(mapping: dict[Any, Any], limit: int, label: str) -> dict[Any, Any]:
    """Keep only the first N items from a mapping to bound fan-out work."""
    if len(mapping) <= limit:
        return mapping

    logger.warning("Truncating %s from %s to %s items to keep enrichment bounded.", label, len(mapping), limit)
    return dict(list(mapping.items())[:limit])


def _limit_list_items(items: list[Any], limit: int, label: str) -> list[Any]:
    """Keep only the first N items from a list to bound fan-out work."""
    if len(items) <= limit:
        return items

    logger.warning("Truncating %s from %s to %s items to keep enrichment bounded.", label, len(items), limit)
    return items[:limit]


def _resolve_enrich_top_k(top_k: int) -> int:
    """Clamp top_k to a safe positive range."""
    if top_k <= 0:
        logger.warning("Invalid enrich top_k=%s. Falling back to default=%s.", top_k, ENRICH_TOP_K)
        return ENRICH_TOP_K

    if top_k > ENRICH_MAX_CANDIDATES_PER_ITEM:
        logger.warning(
            "Clamping enrich top_k from %s to %s to keep enrichment bounded.",
            top_k,
            ENRICH_MAX_CANDIDATES_PER_ITEM,
        )
        return ENRICH_MAX_CANDIDATES_PER_ITEM

    return top_k


def _sort_and_limit_candidate_scores(
    candidate_scores: dict[AttributeKey, float],
    limit: int,
) -> dict[AttributeKey, float]:
    """Return the highest-scoring candidates up to the requested limit."""
    if len(candidate_scores) <= limit:
        return candidate_scores

    ranked_items = sorted(candidate_scores.items(), key=lambda item: item[1], reverse=True)
    return dict(ranked_items[:limit])


def _normalize_temporal_value(value: Any) -> str | None:
    """Coerce temporal values from Neo4j/Python objects into comparable string form."""
    if value is None:
        return None

    try:
        value = sanitize_neo4j_types(value)
    except Exception:
        logger.error("Failed to sanitize temporal value of type %s", type(value).__name__, exc_info=True)

    if value is None:
        return None

    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None

    if isinstance(value, (datetime, date)):
        return value.isoformat()

    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        try:
            normalized = isoformat()
        except TypeError:
            normalized = None
        if isinstance(normalized, str):
            cleaned = normalized.strip()
            if cleaned:
                return cleaned

    cleaned = str(value).strip()
    return cleaned or None


def _normalize_temporal_bound_value(value: Any, is_end: bool) -> str | None:
    """Normalize temporal bounds, expanding bare years to ISO range edges."""
    normalized = _normalize_temporal_value(value)
    if normalized and re.fullmatch(r"\d{4}", normalized):
        return f"{normalized}-12-31T23:59:59Z" if is_end else f"{normalized}-01-01T00:00:00Z"
    return normalized


def _normalize_reasoning_input_item(item: list[AttributeKey] | NodeReasoningItem) -> NodeReasoningItem:
    """Normalize legacy and temporal-aware reasoning items to a unified shape."""
    if isinstance(item, dict):
        req_attrs = _normalize_req_attrs(item.get("req_attrs", []))
        return {
            "req_attrs": req_attrs,
            "effective_from": _normalize_temporal_value(item.get("effective_from")),
            "effective_to": _normalize_temporal_value(item.get("effective_to")),
        }

    return {
        "req_attrs": _normalize_req_attrs(item),
        "effective_from": None,
        "effective_to": None,
    }


def _normalize_reasoning_input_map(input_data: NodeReasoningInput) -> dict[NodeId, NodeReasoningItem]:
    """Normalize reasoning input while preserving backward compatibility."""
    normalized: dict[NodeId, NodeReasoningItem] = {}
    for node_id, item in input_data.items():
        if not node_id:
            continue
        normalized[node_id] = _normalize_reasoning_input_item(item)
    return _limit_mapping_items(normalized, ENRICH_MAX_INPUT_ITEMS, "node enrichment input")


def _extract_reasoning_attribute_keys(input_data: dict[NodeId, NodeReasoningItem]) -> NodeAttributeKeysMap:
    """Project normalized reasoning items down to their requested attributes."""
    return {node_id: list(item.get("req_attrs", [])) for node_id, item in input_data.items()}


def _resolve_similarity_threshold(similarity_threshold: float | None) -> float:
    """Resolve optional similarity threshold to the configured default."""
    return similarity_threshold if similarity_threshold is not None else ENRICH_SIMILARITY_THRESHOLD


def _search_related_attributes(
    attribute_keys: list[AttributeKey],
    similarity_threshold: float = ENRICH_SIMILARITY_THRESHOLD,
) -> dict[AttributeKey, dict[AttributeKey, float]]:
    """
    Search Milvus for semantically related attribute keys.

    Args:
        attribute_keys: List of attribute keys to find related keys for.
        similarity_threshold: Minimum similarity score to include a match.

    Returns:
        Mapping of original_key -> {related_key: similarity_score}.
        Only includes matches above the specified threshold.
    """
    if not attribute_keys:
        return {}

    # Ensure connection
    if not connect_milvus():
        logger.error("Failed to connect to Milvus.")
        return {}

    # Generate embeddings for all keys
    embeddings = get_embedding(attribute_keys)

    # Search Milvus
    try:
        collection = Collection(MILVUS_PROPERTY_COLLECTION)
        collection.load()

        search_params = {"metric_type": "COSINE", "params": {"ef": HNSW_EF_SEARCH}}

        search_results = collection.search(
            data=embeddings,
            anns_field="embedding",
            param=search_params,
            limit=ENRICH_SEARCH_LIMIT,
            output_fields=["property_key"],
        )
    except Exception as e:
        logger.error(f"Milvus search failed: {e}")
        # Return empty if collection doesn't exist or other error
        return {}

    logger.info(f"Milvus search completed for {len(search_results)} queries.")

    # Build related_map with score tracking
    related_map: dict[AttributeKey, dict[AttributeKey, float]] = {key: {} for key in attribute_keys}

    for i, original_key in enumerate(attribute_keys):
        for result in search_results[i]:
            distance = result.distance  # distance property for hits

            # Apply similarity threshold
            if distance < similarity_threshold:
                continue

            found_key = result.entity.get("property_key")

            # Skip self-matches
            if not found_key or found_key == original_key:
                continue

            # Keep maximum score for each related key
            current_score = related_map[original_key].get(found_key, 0)
            if distance > current_score:
                related_map[original_key][found_key] = distance

    return related_map


def _build_candidate_batch(
    input_data: NodeAttributeKeysMap,
    related_map: dict[AttributeKey, dict[AttributeKey, float]],
) -> tuple[list[dict[str, Any]], dict[NodeId, dict[AttributeKey, float]]]:
    """
    Build batch for Neo4j validation, excluding input attributes.

    Args:
        input_data: Original input mapping of node IDs to attributes.
        related_map: Mapping of attribute keys to their related keys with scores.

    Returns:
        Tuple of (neo4j_batch, node_candidate_scores).
        - neo4j_batch: List of {id, candidates} for Neo4j query.
        - node_candidate_scores: Mapping of node_id -> {candidate_key: score}.
    """
    node_candidate_scores: dict[NodeId, dict[AttributeKey, float]] = {}
    neo4j_batch: list[dict[str, Any]] = []

    for node_id, attrs in input_data.items():
        # Aggregate candidates from all input attributes
        candidates_scores: dict[AttributeKey, float] = {}

        for attr in attrs:
            for rel_key, score in related_map.get(attr, {}).items():
                # Use max score when multiple input attributes point to same candidate
                candidates_scores[rel_key] = max(candidates_scores.get(rel_key, 0), score)

        if not candidates_scores:
            continue

        # Filter out attributes already present in input
        final_candidates = {key: score for key, score in candidates_scores.items() if key not in attrs}

        if final_candidates:
            limited_candidates = _sort_and_limit_candidate_scores(final_candidates, ENRICH_MAX_CANDIDATES_PER_ITEM)
            node_candidate_scores[node_id] = limited_candidates
            neo4j_batch.append({"id": node_id, "candidates": list(limited_candidates.keys())})

    return neo4j_batch, node_candidate_scores


def _validate_and_rank_attributes(
    neo4j_batch: list[dict[str, Any]],
    node_candidate_scores: dict[NodeId, dict[AttributeKey, float]],
    top_k: int,
) -> NodeAttributeValuesMap:
    """
    Validate candidate attributes in Neo4j and return top-K ranked results.

    Args:
        neo4j_batch: List of {id, candidates} for Neo4j query.
        node_candidate_scores: Mapping of node_id -> {candidate_key: score}.
        top_k: Number of top results to return per node.

    Returns:
        Mapping of NodeId -> Enriched Attributes.
    """
    enriched_results: NodeAttributeValuesMap = {}

    with Neo4jUtils() as neo4j:
        logger.info(f"Validating candidates for {len(neo4j_batch)} nodes in Neo4j.")
        records = neo4j.run_query(ATTRIBUTE_VALIDATION_QUERY, {"batch": neo4j_batch})

        # Group validated attributes by node
        node_valid_items: dict[NodeId, list[ScoredCandidate]] = {}

        for record in records:
            node_id = record["node_id"]
            key = record["key"]
            value = record["value"]

            # Retrieve original similarity score
            score = node_candidate_scores.get(node_id, {}).get(key, 0)

            if node_id not in node_valid_items:
                node_valid_items[node_id] = []
            node_valid_items[node_id].append((key, value, score))

        # Sort by score and take top-K for each node
        for node_id, items in node_valid_items.items():
            items.sort(key=lambda x: x[2], reverse=True)
            top_items = items[:top_k]
            result_dict = {item[0]: item[1] for item in top_items}
            enriched_results[node_id] = result_dict

        logger.info(f"Found {len(enriched_results)} enriched nodes with valid attributes.")

    return enriched_results


def _fetch_attributes(
    input_data: NodeAttributeKeysMap,
) -> NodeAttributeValuesMap:
    """
    Fetch actual values for the specified attributes from Neo4j.

    Args:
        input_data: Mapping of node IDs to the list of attribute keys to fetch.

    Returns:
        Mapping of NodeId -> {AttributeKey: AttributeValue}
    """
    if not input_data:
        return {}

    batch = [{"id": nid, "attrs": attrs} for nid, attrs in input_data.items()]

    fetched_values: NodeAttributeValuesMap = {}

    try:
        with Neo4jUtils() as neo4j:
            logger.info(f"Fetching original attribute values for {len(batch)} nodes.")
            records = neo4j.run_query(FETCH_ATTRIBUTES_QUERY, {"batch": batch})

            for record in records:
                nid = record["node_id"]
                key = record["attr"]
                val = record["value"]

                if nid not in fetched_values:
                    fetched_values[nid] = {}
                fetched_values[nid][key] = val
    except Exception as e:
        logger.error(f"Error fetching original attributes: {e}", exc_info=True)
        raise

    return fetched_values


# Public API


def find_siblings_with_attributes(
    input_data: NodeReasoningInput,
) -> list[dict[str, Any]]:
    """
    Find unique sibling nodes that share at least one required attribute.

    Definition of Sibling:
    1. Nodes sharing a common parent (Shared Incoming: Sibling <- Parent -> Node)
    2. Nodes sharing a common child (Shared Outgoing: Sibling -> Child <- Node)

    Constraints:
    - Sibling MUST have the same labels (type) as the input node.
    - Sibling MUST possess AT LEAST ONE of the required attributes.

    Args:
        input_data: Dictionary where keys are target node IDs
                    and values are dictionaries of attributes.

    Returns:
        A flat list of unique sibling node properties.

    Raises:
        Exception: If Neo4j query fails.
    """
    if not input_data:
        return []

    normalized_input = _normalize_reasoning_input_map(input_data)

    # Prepare batch parameters
    batch_params = [
        {
            "id": node_id,
            "req_attrs": item["req_attrs"],
            "effective_from": item.get("effective_from"),
            "effective_to": item.get("effective_to"),
        }
        for node_id, item in normalized_input.items()
    ]

    results: list[dict[str, Any]] = []
    try:
        with Neo4jUtils() as neo4j:
            logger.info(f"Executing sibling search for {len(batch_params)} nodes.")
            records = neo4j.run_query(SIBLING_SEARCH_QUERY, {"batch": batch_params})
            # logger.error(f"Found sibling nodes: {records}")
            deduped_results: list[dict[str, Any]] = []
            seen: set[str] = set()

            for record in records:
                source_node_id = record["source_node_id"]
                source_item = normalized_input.get(source_node_id, {})

                if not check_temporal_overlap(
                    entity_from=_normalize_temporal_value(record.get("sibling_effective_from")),
                    entity_to=_normalize_temporal_value(record.get("sibling_effective_to")),
                    query_from=_normalize_temporal_value(source_item.get("effective_from")),
                    query_to=_normalize_temporal_value(source_item.get("effective_to")),
                ):
                    continue

                props = sanitize_neo4j_types(record["props"])
                dedupe_key = json.dumps(props, ensure_ascii=False, sort_keys=True, default=str)
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                deduped_results.append(props)

            results = deduped_results
            logger.info(f"Found {len(results)} unique siblings.")

    except Exception as e:
        logger.error(f"Error finding siblings: {e}", exc_info=True)
        raise

    return results


def _recommend_attributes(
    input_data: dict[NodeId, NodeReasoningItem],
    similarity_threshold: float,
) -> tuple[list[dict[str, Any]], dict[NodeId, dict[AttributeKey, float]]]:
    """
    Generate attribute recommendations using Milvus based on node labels and input attributes.

    Args:
        input_data: Node IDs and their existing attributes.
        similarity_threshold: Threshold for vector similarity.

    Returns:
        Tuple of (neo4j_batch, node_candidate_scores).
    """
    try:
        # Step 1: Fetch node labels
        node_labels_map = _fetch_node_labels(list(input_data.keys()))

        # Step 2: Group nodes by labels
        grouped_input: dict[frozenset, NodeAttributeKeysMap] = {}
        unique_keys_cache: dict[tuple[tuple[NodeId, tuple[AttributeKey, ...]], ...], list[AttributeKey]] = {}
        related_map_cache: dict[
            tuple[frozenset, tuple[AttributeKey, ...], float], dict[AttributeKey, dict[AttributeKey, float]]
        ] = {}

        attribute_input = _extract_reasoning_attribute_keys(input_data)

        for node_id, attrs in attribute_input.items():
            labels = node_labels_map.get(node_id, frozenset())
            if labels not in grouped_input:
                grouped_input[labels] = {}
            grouped_input[labels][node_id] = attrs

        # Prepare aggregated data
        all_neo4j_batch: list[dict[str, Any]] = []
        all_node_candidate_scores: dict[NodeId, dict[AttributeKey, float]] = {}

        # Step 3: Process each group
        for labels, group_data in grouped_input.items():
            logger.info(f"Processing group with labels {list(labels)}: {len(group_data)} nodes")

            # a. Collect unique keys
            group_signature = tuple(sorted((node_id, tuple(attrs)) for node_id, attrs in group_data.items()))
            unique_keys = unique_keys_cache.get(group_signature)
            if unique_keys is None:
                unique_keys = _collect_unique_keys(group_data)
                unique_keys_cache[group_signature] = unique_keys
            if not unique_keys:
                continue

            # b. Search Milvus
            try:
                related_map_key = (labels, tuple(unique_keys), similarity_threshold)
                related_map = related_map_cache.get(related_map_key)
                if related_map is None:
                    related_map = _search_related_attributes(unique_keys, similarity_threshold)
                    related_map_cache[related_map_key] = related_map
            except Exception as e:
                logger.error(f"Error searching Milvus for group {labels}: {e}")
                continue

            # c. Build candidate batch
            neo4j_batch, node_candidate_scores = _build_candidate_batch(group_data, related_map)

            all_neo4j_batch.extend(neo4j_batch)
            all_node_candidate_scores.update(node_candidate_scores)

        return all_neo4j_batch, all_node_candidate_scores

    except Exception as e:
        logger.error(f"Error in _recommend_attributes: {e}", exc_info=True)
        raise


def enrich_attribute(
    input_data: NodeAttributeKeysMap,
    top_k: int = ENRICH_TOP_K,
    similarity_threshold: float | None = None,
) -> NodeAttributeValuesMap:
    """
    Enrich node attributes by finding semantically related attributes.
    """
    if not input_data:
        return {}

    safe_top_k = _resolve_enrich_top_k(top_k)
    logger.info(f"Starting enrichment for {len(input_data)} nodes (top_k=%s).", safe_top_k)

    # Resolve threshold
    threshold = _resolve_similarity_threshold(similarity_threshold)

    try:
        # 1. Get Recommendations
        neo4j_batch, node_candidate_scores = _recommend_attributes(
            _normalize_reasoning_input_map(cast(NodeReasoningInput, input_data)),
            threshold,
        )

        if not neo4j_batch:
            logger.info("No candidates found after type-aware enrichment.")
            return {}

        # 2. Validate in Neo4j (Option 1.1 Logic mainly uses this)
        return _validate_and_rank_attributes(neo4j_batch, node_candidate_scores, safe_top_k)

    except Exception as e:
        logger.error(f"Error in enrich_attribute: {e}", exc_info=True)
        raise


def build_enriched_criteria_map(
    neo4j_batch: list[dict[str, Any]],
    input_data: dict[NodeId, NodeReasoningItem],
    top_k: int,
    node_candidate_scores: dict[NodeId, dict[AttributeKey, float]] | None = None,
) -> NodeReasoningInput:
    """Build sibling-search criteria from candidate batches."""
    enriched_criteria_map: NodeReasoningInput = {}
    safe_top_k = _resolve_enrich_top_k(top_k)
    for item in neo4j_batch:
        source_item = input_data.get(item["id"], {})
        scored_candidates = (node_candidate_scores or {}).get(item["id"])
        if scored_candidates:
            candidate_keys = list(_sort_and_limit_candidate_scores(scored_candidates, safe_top_k).keys())
        else:
            candidate_keys = item["candidates"][:safe_top_k]

        if not candidate_keys:
            continue

        enriched_criteria_map[item["id"]] = {
            "req_attrs": candidate_keys,
            "effective_from": source_item.get("effective_from"),
            "effective_to": source_item.get("effective_to"),
        }
    return enriched_criteria_map


def run_node_enrichment_pipeline(
    input_data: NodeReasoningInput,
    enrich_top_k: int,
    similarity_threshold: float | None,
) -> dict[str, Any]:
    """Run the node reasoning pipeline without mutating external state."""
    safe_top_k = _resolve_enrich_top_k(enrich_top_k)
    threshold = _resolve_similarity_threshold(similarity_threshold)
    normalized_input = _normalize_reasoning_input_map(input_data)
    attribute_input = _extract_reasoning_attribute_keys(normalized_input)

    neo4j_batch, node_candidate_scores = _recommend_attributes(normalized_input, threshold)
    enriched_criteria_map = build_enriched_criteria_map(
        neo4j_batch,
        normalized_input,
        safe_top_k,
        node_candidate_scores,
    )

    option_1_1_result: NodeAttributeValuesMap = {}
    if neo4j_batch:
        option_1_1_result = _validate_and_rank_attributes(neo4j_batch, node_candidate_scores, safe_top_k)

    option_1_2_result: list[dict[str, Any]] = []
    option_2_result: list[dict[str, Any]] = []
    if enriched_criteria_map:
        option_1_2_result = find_siblings_with_attributes(enriched_criteria_map)

    if option_1_2_result:
        logger.info(f"Using Option 1.2: Found {len(option_1_2_result)} siblings via enriched attributes.")
    else:
        logger.info("Option 1.2 empty. Falling back to Option 2 (Original Attributes).")
        option_2_result = find_siblings_with_attributes(cast(NodeReasoningInput, attribute_input))

    return {
        "option_1_1": option_1_1_result,
        "option_1_2": option_1_2_result,
        "option_2": option_2_result,
    }


def persist_node_reasoning_state(tool_context: ToolContext, final_output: dict[str, Any]) -> None:
    """Persist node reasoning results to ToolContext state."""
    try:
        tool_context.state["data_lv2_data_enriched_nodes"] = json.dumps(
            final_output["option_1_1"],
            ensure_ascii=False,
            default=str,
        )
        tool_context.state["data_lv2_siblings_relative"] = json.dumps(
            final_output["option_1_2"],
            ensure_ascii=False,
            default=str,
        )
        tool_context.state["data_lv2_siblings_similar"] = json.dumps(
            final_output["option_2"],
            ensure_ascii=False,
            default=str,
        )
    except Exception as e:
        logger.error(f"Failed to store reasoning data in ToolContext: {e}")


def process_reasoning(
    tool_context: Any,
    input_data: NodeReasoningInput,
    enrich_top_k: int = ENRICH_TOP_K,
    similarity_threshold: float | None = None,
) -> dict[str, Any]:
    """
    Complete Reasoning Pipeline with 3-Option Priority Logic:

    Priority 1 (Enriched Context):
    - Option 1.1: Original Node + Enriched Attributes (Validated)
    - Option 1.2: Sibling Nodes + Enriched Attributes (Search Criteria)

    Priority 2 (Fallback - Original Context):
    - Option 2: Sibling Nodes + Original Attributes

    Logic:
    If (Option 1.1 has data OR Option 1.2 has data):
        Return results from 1.1 and 1.2.
    Else:
        Return results from Option 2.

    Args:
        input_data: Initial nodes and their known attributes.
        enrich_top_k: Top-K related attributes to retrieve.
        similarity_threshold: Similarity cutoff.

    Returns:
        Dictionary containing:
        - 'option_1_1': Enriched attributes on original nodes.
        - 'option_1_2': Siblings found using enriched attributes.
        - 'option_2': Siblings found using original attributes (Fallback).
        - 'selected_strategy': 'enriched' or 'original_fallback'.
        - 'enriched_nodes': (Alias for option_1_1 for backward compatibility)
        - 'siblings': (Alias for selected siblings for backward compatibility)
    """
    logger.info("Starting reasoning processing pipeline (3-Option Logic)...")

    final_output = run_node_enrichment_pipeline(
        input_data=input_data,
        enrich_top_k=enrich_top_k,
        similarity_threshold=similarity_threshold,
    )

    logger.info("Reasoning pipeline completed.")

    persist_node_reasoning_state(tool_context, final_output)

    return final_output


# Relation Attribute Enrichment




class RelationEnrichItem(TypedDict):
    """Input item describing a single relation to enrich.

    Attributes:
        relation_id: Neo4j element ID (``elementId(r)``). Used to uniquely
            identify the relationship because multiple edges with the same
            label can exist between the same pair of nodes.
        relation_label: Relationship type (e.g. ``"HAS_POSITION"``).
        relation_attributes: Attribute keys already present on the relation.
        source_node_id: ``id`` property of the source node.
        destination_node_id: ``id`` property of the destination node.
        source_node_attributes: Optional attribute keys on the source node,
            used by Step 2.2 (fallback source‑node enrichment).
    """

    relation_id: str
    relation_label: str
    relation_attributes: list[str]
    source_node_id: str
    destination_node_id: str
    source_node_attributes: list[str] | None


class RelationReasoningInput(TypedDict, total=False):
    items: list[RelationEnrichItem]
    effective_from: TemporalValue
    effective_to: TemporalValue



# Milvus collection for relation attribute embeddings
MILVUS_RELATION_COLLECTION = "knowledge_university_relation"

TRIANGLE_NODE_RETURN_ATTRIBUTES = ("name", "description", "address", "combination_detail")



RELATION_ATTRIBUTE_VALIDATION_QUERY = """
UNWIND $batch AS item
MATCH ()-[r]->()
WHERE elementId(r) = item.relation_id
UNWIND item.candidates AS key
WITH item.relation_id AS relation_id, r, key
WHERE r[key] IS NOT NULL
RETURN relation_id, key, r[key] AS value
LIMIT 5
"""

SIBLING_RELATION_QUERY = """
UNWIND $batch AS item
MATCH (src {id: item.source_node_id})
MATCH (sibling)-[r]->(dest {id: item.destination_node_id})
WHERE type(r) = item.relation_label
  AND elementId(r) <> item.relation_id
  AND labels(sibling) = labels(src)
  AND sibling <> src
WITH item.relation_id AS source_relation_id, sibling, r, coalesce(item.relation_attributes, []) AS req_attrs
WITH source_relation_id, sibling, r, req_attrs,
     [attr IN req_attrs WHERE r[attr] IS NOT NULL | [attr, r[attr]]] AS rel_pairs
WHERE size(req_attrs) = 0 OR size(rel_pairs) > 0
RETURN DISTINCT sibling.name AS sibling_name,
       source_relation_id,
       sibling.id AS sibling_node_id,
       apoc.map.fromPairs(rel_pairs) AS relation_properties,
       sibling.effective_from AS sibling_effective_from,
       sibling.effective_to AS sibling_effective_to,
       r.effective_from AS relation_effective_from,
       r.effective_to AS relation_effective_to
LIMIT 5
"""

SIBLING_RELATION_DEST_QUERY = """
UNWIND $batch AS item
MATCH (dest {id: item.destination_node_id})
MATCH (src {id: item.source_node_id})-[r]->(sibling)
WHERE type(r) = item.relation_label
  AND elementId(r) <> item.relation_id
  AND labels(sibling) = labels(dest)
  AND sibling <> dest
WITH item.relation_id AS source_relation_id, sibling, r, coalesce(item.relation_attributes, []) AS req_attrs
WITH source_relation_id, sibling, r, req_attrs,
     [attr IN req_attrs WHERE r[attr] IS NOT NULL | [attr, r[attr]]] AS rel_pairs
WHERE size(req_attrs) = 0 OR size(rel_pairs) > 0
RETURN DISTINCT sibling.name AS sibling_name,
       source_relation_id,
       sibling.id AS sibling_node_id,
       apoc.map.fromPairs(rel_pairs) AS relation_properties,
       sibling.effective_from AS sibling_effective_from,
       sibling.effective_to AS sibling_effective_to,
       r.effective_from AS relation_effective_from,
       r.effective_to AS relation_effective_to
LIMIT 5
"""

TRIANGLE_NODE_QUERY = """
UNWIND $batch AS item
MATCH (src {id: item.source_node_id})
MATCH (dest {id: item.destination_node_id})
MATCH (src)-[]-(n)-[]-(dest)
WHERE n <> src AND n <> dest
WITH DISTINCT n
RETURN n.id AS node_id, n.name AS name,
       n.description AS description,
       n.bio AS bio, n.address AS address, n.combination_detail AS combination_detail
LIMIT 5
"""




def _search_related_relation_attributes(
    attribute_keys: list[AttributeKey],
    source_node_id: str,
    destination_node_id: str,
    similarity_threshold: float = ENRICH_SIMILARITY_THRESHOLD,
) -> dict[AttributeKey, dict[AttributeKey, float]]:
    """Search Milvus for semantically related relation attribute keys.

    Mirrors ``_search_related_attributes`` but targets the relation
    collection (``embedding_attribute`` field). Applies a filter on
    ``source_node_id`` and ``destination_node_id`` to restrict results
    to the same pair of nodes, reducing search scope.

    Args:
        attribute_keys: Attribute keys to find related keys for.
        source_node_id: Source node ID for Milvus filter.
        destination_node_id: Destination node ID for Milvus filter.
        similarity_threshold: Minimum cosine similarity to accept.

    Returns:
        Mapping of original_key -> {related_key: similarity_score}.
    """
    if not attribute_keys:
        return {}

    if not connect_milvus():
        logger.error("Failed to connect to Milvus for relation attribute search.")
        return {}

    embeddings = get_embedding(attribute_keys)

    try:
        collection = Collection(MILVUS_RELATION_COLLECTION)
        collection.load()

        search_params = {"metric_type": "COSINE", "params": {"ef": HNSW_EF_SEARCH}}

        # Filter to relations between the same source-destination pair
        filter_expr = f'source_node_id == "{source_node_id}" and destination_node_id == "{destination_node_id}"'

        search_results = collection.search(
            data=embeddings,
            anns_field="embedding_attribute",
            param=search_params,
            limit=ENRICH_SEARCH_LIMIT,
            expr=filter_expr,
            output_fields=["attribute"],
        )
    except Exception as e:
        logger.error(f"Milvus relation attribute search failed: {e}")
        return {}

    logger.info(
        f"Milvus relation attribute search completed for {len(search_results)} queries "
        f"(filter: {source_node_id} -> {destination_node_id})."
    )

    related_map: dict[AttributeKey, dict[AttributeKey, float]] = {key: {} for key in attribute_keys}

    for i, original_key in enumerate(attribute_keys):
        for result in search_results[i]:
            distance = result.distance

            if distance < similarity_threshold:
                continue

            found_key = result.entity.get("attribute")

            if not found_key or found_key == original_key:
                continue

            current_score = related_map[original_key].get(found_key, 0)
            if distance > current_score:
                related_map[original_key][found_key] = distance

    return related_map


def _normalize_relation_enrich_item(item: RelationEnrichItem) -> RelationEnrichItem:
    """Normalize a relation reasoning item to the canonical relation shape."""
    return {
        "relation_id": str(item["relation_id"]),
        "relation_label": str(item["relation_label"]),
        "relation_attributes": _normalize_req_attrs(item.get("relation_attributes") or []),
        "source_node_id": str(item["source_node_id"]),
        "destination_node_id": str(item["destination_node_id"]),
        "source_node_attributes": _normalize_req_attrs(item.get("source_node_attributes") or []),
    }


def _normalize_relation_enrich_items(items: list[RelationEnrichItem]) -> list[RelationEnrichItem]:
    """Normalize relation reasoning inputs to the canonical relation shape."""
    normalized_items: list[RelationEnrichItem] = []
    for item in items:
        if not item:
            continue
        normalized_items.append(_normalize_relation_enrich_item(item))
    return _limit_list_items(normalized_items, ENRICH_MAX_INPUT_ITEMS, "relation enrichment input")


def _enrich_relation_attributes(
    items: list[RelationEnrichItem],
    top_k: int = ENRICH_TOP_K,
    similarity_threshold: float = ENRICH_SIMILARITY_THRESHOLD,
) -> dict[RelationId, dict[AttributeKey, AttributeValue]]:
    """Step 1: Enrich attributes of the given relations.

    Pipeline per item:
        1. Collect attribute keys from the relation.
        2. Search Milvus for semantically similar keys (filtered by
           source_node_id + destination_node_id).
        3. Build candidate list, excluding already-present attributes.
        4. Validate candidates exist on the relation in Neo4j.
        5. Rank by similarity score and return top-K.

    Args:
        items: Relations to enrich.
        top_k: Maximum enriched attributes per relation.
        similarity_threshold: Minimum cosine similarity for Milvus search.

    Returns:
        Mapping of relation_id -> {enriched_attr_key: attr_value}.
    """
    if not items:
        return {}

    safe_top_k = _resolve_enrich_top_k(top_k)
    normalized_items = _normalize_relation_enrich_items(items)

    # Process each item individually (each may have different source/dest pair)
    neo4j_batch: list[dict[str, Any]] = []
    relation_candidate_scores: dict[RelationId, dict[AttributeKey, float]] = {}
    related_map_cache: dict[RelationAttributeSearchKey, dict[AttributeKey, dict[AttributeKey, float]]] = {}

    for item in normalized_items:
        attr_keys = item["relation_attributes"]
        if not attr_keys:
            continue

        # 1-2. Search Milvus with source/dest filter
        cache_key = (item["source_node_id"], item["destination_node_id"], tuple(attr_keys))
        related_map = related_map_cache.get(cache_key)
        if related_map is None:
            related_map = _search_related_relation_attributes(
                attr_keys,
                source_node_id=item["source_node_id"],
                destination_node_id=item["destination_node_id"],
                similarity_threshold=similarity_threshold,
            )
            related_map_cache[cache_key] = related_map

        # 3. Build candidate list, excluding already-present attributes
        candidates_scores: dict[AttributeKey, float] = {}
        existing_attrs = set(attr_keys)

        for attr in attr_keys:
            for rel_key, score in related_map.get(attr, {}).items():
                if rel_key in existing_attrs:
                    continue
                candidates_scores[rel_key] = max(candidates_scores.get(rel_key, 0), score)

        if not candidates_scores:
            continue

        rid = item["relation_id"]
        limited_candidates = _sort_and_limit_candidate_scores(candidates_scores, ENRICH_MAX_CANDIDATES_PER_ITEM)
        relation_candidate_scores[rid] = limited_candidates
        neo4j_batch.append({"relation_id": rid, "candidates": list(limited_candidates.keys())})

    if not neo4j_batch:
        logger.info("No relation attribute candidates found after Milvus search.")
        return {}

    # 4. Validate in Neo4j
    enriched_results: dict[RelationId, dict[AttributeKey, AttributeValue]] = {}

    with Neo4jUtils() as neo4j:
        logger.info(f"Validating relation attribute candidates for {len(neo4j_batch)} relations.")
        records = neo4j.run_query(RELATION_ATTRIBUTE_VALIDATION_QUERY, {"batch": neo4j_batch})

        # Group validated attributes by relation
        relation_valid_items: dict[RelationId, list[ScoredCandidate]] = {}

        for record in records:
            rid = record["relation_id"]
            key = record["key"]
            value = record["value"]
            score = relation_candidate_scores.get(rid, {}).get(key, 0)

            if rid not in relation_valid_items:
                relation_valid_items[rid] = []
            relation_valid_items[rid].append((key, value, score))

        # 5. Rank and take top-K
        for rid, scored_items in relation_valid_items.items():
            scored_items.sort(key=lambda x: x[2], reverse=True)
            top_items = scored_items[:safe_top_k]
            enriched_results[rid] = {item[0]: item[1] for item in top_items}

        logger.info(f"Enriched {len(enriched_results)} relations with additional attributes.")

    return enriched_results


def _find_sibling_relations(
    items: list[RelationEnrichItem],
    find_dest_sibling: bool = False,
    effective_from: TemporalValue = None,
    effective_to: TemporalValue = None,
) -> list[dict[str, Any]]:
    """Step 2.1: Find sibling nodes of source/dest with the same relation label.

    If find_dest_sibling is False (Step 2.1.1):
      Searches for other nodes (siblings of source) that also have a relationship
      with the same label pointing to the same destination node.
    If find_dest_sibling is True (Step 2.1.2):
      Searches for other nodes (siblings of dest) that also have a relationship
      with the same label from the same source node.

    Args:
        items: Relations whose sibling to find.
        find_dest_sibling: Whether to find sibling of destination node instead of source.

    Returns:
        List of sibling dicts, each containing ``sibling_name``,
        ``sibling_node_id``, and ``relation_properties``.
    """
    if not items:
        return []

    normalized_items = _normalize_relation_enrich_items(items)
    batch_params = _build_relation_sibling_batch(normalized_items)

    query = SIBLING_RELATION_DEST_QUERY if find_dest_sibling else SIBLING_RELATION_QUERY
    query_effective_from = _normalize_temporal_bound_value(effective_from, is_end=False)
    query_effective_to = _normalize_temporal_bound_value(effective_to, is_end=True)

    try:
        with Neo4jUtils() as neo4j:
            logger.info(
                f"Searching sibling relations for {len(batch_params)} items (find_dest_sibling={find_dest_sibling})."
            )
            records = neo4j.run_query(query, {"batch": batch_params})
            results: list[dict[str, Any]] = []

            for record in records:
                relation_overlaps = check_temporal_overlap(
                    entity_from=_normalize_temporal_value(record.get("relation_effective_from")),
                    entity_to=_normalize_temporal_value(record.get("relation_effective_to")),
                    query_from=query_effective_from,
                    query_to=query_effective_to,
                )
                if not relation_overlaps:
                    continue

                sibling_overlaps = check_temporal_overlap(
                    entity_from=_normalize_temporal_value(record.get("sibling_effective_from")),
                    entity_to=_normalize_temporal_value(record.get("sibling_effective_to")),
                    query_from=query_effective_from,
                    query_to=query_effective_to,
                )
                if not sibling_overlaps:
                    continue

                results.append(_map_sibling_relation_record(record))

            logger.info(f"Found {len(results)} sibling relations.")
    except Exception as e:
        logger.error(f"Error finding sibling relations: {e}", exc_info=True)
        raise

    return results


def _enrich_source_node_attributes(
    items: list[RelationEnrichItem],
    top_k: int = ENRICH_TOP_K,
    similarity_threshold: float = ENRICH_SIMILARITY_THRESHOLD,
) -> NodeAttributeValuesMap:
    """Step 2.2: Enrich attributes of the source node (fallback).

    Delegates to the existing ``enrich_attribute`` function using
    ``source_node_attributes`` from each item.

    Args:
        items: Relations whose source nodes to enrich.
        top_k: Maximum enriched attributes per node.
        similarity_threshold: Minimum cosine similarity.

    Returns:
        Mapping of source_node_id -> {enriched_attr_key: attr_value}.
    """
    normalized_items = _normalize_relation_enrich_items(items)
    input_data: NodeAttributeKeysMap = {}

    for item in normalized_items:
        source_attrs = item.get("source_node_attributes")
        if not source_attrs:
            continue
        sid = item["source_node_id"]
        if sid not in input_data:
            input_data[sid] = []
        # Merge unique attributes
        existing = set(input_data[sid])
        for attr in source_attrs:
            if attr not in existing:
                input_data[sid].append(attr)
                existing.add(attr)

    if not input_data:
        logger.info("No source node attributes provided for enrichment (Step 2.2).")
        return {}

    return enrich_attribute(input_data, top_k, similarity_threshold)


def _find_triangle_nodes(
    items: list[RelationEnrichItem],
) -> list[dict[str, Any]]:
    """Step 3: Find nodes connected to both source and destination nodes.

    Discovers "triangle" nodes — nodes that have a direct relationship
    (in any direction) to both the source and destination nodes.
    Only returns a fixed set of attributes: name, description, bio, address.

    Args:
        items: Relations whose triangle nodes to find.

    Returns:
        List of triangle node dicts with filtered attributes.
    """
    if not items:
        return []

    batch_params = _build_relation_pair_batch(_normalize_relation_enrich_items(items))

    results: list[dict[str, Any]] = []

    try:
        with Neo4jUtils() as neo4j:
            logger.info(f"Searching triangle nodes for {len(batch_params)} source-dest pairs.")
            records = neo4j.run_query(TRIANGLE_NODE_QUERY, {"batch": batch_params})

            for record in records:
                node_data: dict[str, Any] = {"node_id": record["node_id"]}
                for attr in TRIANGLE_NODE_RETURN_ATTRIBUTES:
                    value = record.get(attr)
                    if value is not None:
                        node_data[attr] = value

                results.append(node_data)

            logger.info(f"Found {len(results)} triangle nodes.")
    except Exception as e:
        logger.error(f"Error finding triangle nodes: {e}", exc_info=True)
        raise

    return results


def _build_relation_sibling_batch(items: list[RelationEnrichItem]) -> list[dict[str, Any]]:
    """Build batch params shared by relation sibling queries."""
    return [
        {
            "destination_node_id": item["destination_node_id"],
            "source_node_id": item["source_node_id"],
            "relation_label": item["relation_label"],
            "relation_id": item["relation_id"],
            "relation_attributes": item["relation_attributes"],
        }
        for item in items
    ]


def _build_relation_pair_batch(items: list[RelationEnrichItem]) -> list[dict[str, Any]]:
    """Build source-destination pair batch for relation queries."""
    return [
        {
            "source_node_id": item["source_node_id"],
            "destination_node_id": item["destination_node_id"],
        }
        for item in items
    ]


def _map_sibling_relation_record(record: dict[str, Any]) -> dict[str, Any]:
    """Normalize a sibling relation query record into response payload."""
    return {
        "sibling_name": record["sibling_name"],
        "sibling_node_id": record["sibling_node_id"],
        "relation_properties": record["relation_properties"],
    }


def run_relation_step_1(
    items: list[RelationEnrichItem],
    enrich_top_k: int,
    similarity_threshold: float,
) -> dict[RelationId, dict[AttributeKey, AttributeValue]]:
    """Run step 1 of relation reasoning."""
    return _enrich_relation_attributes(items, enrich_top_k, similarity_threshold)


def run_relation_step_2_1(
    items: list[RelationEnrichItem],
    effective_from: TemporalValue = None,
    effective_to: TemporalValue = None,
) -> list[dict[str, Any]]:
    """Run step 2.1 relation sibling discovery with current branching order."""
    step_2_1_result = _find_sibling_relations(
        items,
        find_dest_sibling=False,
        effective_from=effective_from,
        effective_to=effective_to,
    )
    if not step_2_1_result:
        logger.info("Step 2.1.1 empty  proceeding to Step 2.1.2 (finding destination sibling).")
        step_2_1_result = _find_sibling_relations(
            items,
            find_dest_sibling=True,
            effective_from=effective_from,
            effective_to=effective_to,
        )
    return step_2_1_result


def run_relation_fallback_or_triangle(
    items: list[RelationEnrichItem],
    step_2_1_result: list[dict[str, Any]],
    enrich_top_k: int,
    similarity_threshold: float,
) -> tuple[NodeAttributeValuesMap, list[dict[str, Any]]]:
    """Run the current conditional branch after step 2.1."""
    step_2_2_result: NodeAttributeValuesMap = {}
    step_3_result: list[dict[str, Any]] = []

    if step_2_1_result:
        logger.info(f"Step 2.1 found {len(step_2_1_result)} siblings  proceeding to Step 3.")
        step_3_result = _find_triangle_nodes(items)
    else:
        logger.info("Step 2.1 empty  falling back to Step 2.2 (source node enrichment).")
        step_2_2_result = _enrich_source_node_attributes(items, enrich_top_k, similarity_threshold)

    return step_2_2_result, step_3_result


def persist_relation_reasoning_state(
    tool_context: ToolContext,
    step_1_result: dict[RelationId, dict[AttributeKey, AttributeValue]],
    step_2_1_result: list[dict[str, Any]],
    step_2_2_result: NodeAttributeValuesMap,
    step_3_result: list[dict[str, Any]],
) -> None:
    """Persist relation reasoning results to ToolContext state."""
    try:
        tool_context.state["data_lv2_data_enriched_relation"] = json.dumps(
            step_1_result,
            ensure_ascii=False,
            default=str,
        )
        tool_context.state["data_lv2_siblings_similar"] = json.dumps(
            step_2_1_result,
            ensure_ascii=False,
            default=str,
        )
        tool_context.state["data_lv2_data_enriched_nodes"] = json.dumps(
            step_2_2_result,
            ensure_ascii=False,
            default=str,
        )
        tool_context.state["data_lv2_related_nodes"] = json.dumps(
            step_3_result,
            ensure_ascii=False,
            default=str,
        )
    except Exception as e:
        logger.error(f"Failed to store relation reasoning data in ToolContext: {e}")




def process_relation_reasoning(
    tool_context: Any,
    input_data: RelationReasoningInput,
    enrich_top_k: int = ENRICH_TOP_K,
    similarity_threshold: float | None = None,
) -> dict[str, Any]:
    """Relation enrichment pipeline with conditional branching.

    Pipeline:
        Step 1:     Enrich relation attributes → ``data_lv2_data_enriched_relation``
        Step 2.1.1: Find sibling relations (source siblings)
        Step 2.1.2: If 2.1.1 is empty, find sibling relations (dest siblings)
                    (Merged 2.1.1 and 2.1.2 → ``data_lv2_siblings_similar``)
          If 2.1.1 or 2.1.2 has results → Step 3
          Else                          → Step 2.2
        Step 2.2:   Enrich source node attrs   → ``data_lv2_data_enriched_nodes``
        Step 3:     Find triangle nodes        → ``data_lv2_related_nodes``

    Args:
        tool_context: ADK tool context for persisting results to state.
        input_data: Relation batch input with top-level temporal bounds.
        enrich_top_k: Top-K related attributes to keep per entity.
        similarity_threshold: Cosine similarity cutoff for Milvus search.

    Returns:
        Dictionary containing all enrichment results keyed by step.
    """
    items = input_data.get("items", [])
    if not items:
        return {}

    logger.info(f"Starting relation reasoning pipeline for {len(items)} relations.")

    threshold = _resolve_similarity_threshold(similarity_threshold)
    normalized_items = _normalize_relation_enrich_items(items)
    normalized_effective_from = _normalize_temporal_bound_value(input_data.get("effective_from"), is_end=False)
    normalized_effective_to = _normalize_temporal_bound_value(input_data.get("effective_to"), is_end=True)

    # Step 1: Enrich relation's own attributes
    step_1_result = run_relation_step_1(normalized_items, enrich_top_k, threshold)

    # Step 2.1.1: Find sibling nodes via same relation label (find source siblings)
    step_2_1_result = run_relation_step_2_1(
        normalized_items,
        effective_from=normalized_effective_from,
        effective_to=normalized_effective_to,
    )
    step_2_2_result, step_3_result = run_relation_fallback_or_triangle(
        items=normalized_items,
        step_2_1_result=step_2_1_result,
        enrich_top_k=enrich_top_k,
        similarity_threshold=threshold,
    )

    final_output: dict[str, Any] = {
        "step_1_enriched_relation": step_1_result,
        "step_2_1_siblings": step_2_1_result,
        "step_2_2_enriched_source": step_2_2_result,
        "step_3_triangle_nodes": step_3_result,
    }

    logger.info("Relation reasoning pipeline completed.")

    persist_relation_reasoning_state(
        tool_context=tool_context,
        step_1_result=step_1_result,
        step_2_1_result=step_2_1_result,
        step_2_2_result=step_2_2_result,
        step_3_result=step_3_result,
    )

    return final_output


# Entry Point (Testing)

if __name__ == "__main__":
    test_input: RelationReasoningInput = {
        "items": [
            {
                "source_node_id": "68e0d61f-8ce3-476c-bd7b-f9318002c637",
                "relation_id": "5:ec56bc79-5a50-4bcb-bb32-72f2659d9427:613",
                "relation_label": "APPLIES_TO",
                "destination_node_id": "42ec39d1-e985-4ebf-b52c-3eac2117cb6a",
                "source_node_attributes": ["code", "description", "type", "document_name", "name", "outcome", "status"],
                "relation_attributes": [],
            }
        ],
        "effective_from": "2026-01-01T00:00:00Z",
        "effective_to": None,
    }

    # Test the full pipeline
    try:
        logger.info("--- Running Complete Reasoning Pipeline ---")
        pipeline_results = process_relation_reasoning(tool_context=None, input_data=test_input)

        import json

        logger.info(json.dumps(pipeline_results, ensure_ascii=False, indent=2, default=str))
    except Exception as e:
        logger.error(f"Failed: {e}")
