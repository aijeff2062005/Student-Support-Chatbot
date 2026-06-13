from __future__ import annotations

import re
import traceback
from typing import TYPE_CHECKING, Any

from utils.logging_config import get_logger

if TYPE_CHECKING:
    from tools.reasoning_processing import RelationEnrichItem, RelationReasoningInput

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Candidate node properties that carry no analytical value for enrichment
_CANDIDATE_PROPS_EXCLUDE: frozenset[str] = frozenset(
    {
        "source_documents",
        "updated_at",
        "created_at",
        "node_type",
        "version",
        "id",
        "effective_from",
        "effective_to",
    }
)
_RELATION_PROPS_EXCLUDE: frozenset[str] = frozenset({"id", "effective_from", "effective_to"})
logger = get_logger(__name__)

type AttributeMatchList = list[dict[str, Any]]
type AttributeMatchMap = dict[str, dict[str, Any]]
type ReasoningInputMap = dict[str, list[str]]
type TemporalReasoningInputMap = dict[str, dict[str, Any]]


def _normalize_temporal_bound(value: Any, is_end: bool) -> str | None:
    """Normalize year-like temporal values to ISO strings for reasoning filters."""
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    if re.fullmatch(r"\d{4}", text):
        return f"{text}-12-31T23:59:59Z" if is_end else f"{text}-01-01T00:00:00Z"

    return text


def _resolve_reasoning_temporal_bounds(
    effective_from: Any | None = None,
    effective_to: Any | None = None,
    time: dict[str, Any] | None = None,
) -> tuple[str | None, str | None]:
    """Resolve explicit or time-dict bounds into a normalized reasoning window."""
    if effective_from is not None or effective_to is not None:
        return _normalize_temporal_bound(effective_from, is_end=False), _normalize_temporal_bound(
            effective_to, is_end=True
        )

    if isinstance(time, dict):
        if time.get("year") is not None:
            year = time.get("year")
            return _normalize_temporal_bound(year, is_end=False), _normalize_temporal_bound(year, is_end=True)

        return _normalize_temporal_bound(time.get("from_year"), is_end=False), _normalize_temporal_bound(
            time.get("to_year"), is_end=True
        )

    return None, None


def extract_unique_node_labels(meta_data_query: dict[str, Any]) -> list[str]:
    """
    Extract all unique node_types (labels) from meta_data_query.
    Works for both Attribute Search and Enumeration Search.

    Args:
        meta_data_query: API response dict containing meta_data_query

    Returns:
        Sorted list of unique node_types

    Examples:
        labels = extract_unique_node_labels(api_response)
        # ['Faculty', 'Major', 'University']
    """
    try:
        logger.info(" Extracting unique node labels from meta_data_query...")
        unique_labels = set()

        # Get meta_data_query
        # meta_data = api_results.get("meta_data_query", {})

        # Extract from target_entities
        target_entities = meta_data_query.get("target_entities", [])
        for target in target_entities:
            node_type = target.get("node_type")
            if node_type:
                unique_labels.add(node_type)

        # Extract from related_entities
        related_entities = meta_data_query.get("related_entities", [])
        for related in related_entities:
            node_type = related.get("node_type")
            if node_type:
                unique_labels.add(node_type)

        # Convert to sorted list
        result = sorted(list(unique_labels))
        logger.info(f"Found {len(result)} unique labels: {result}")
        return result

    except Exception as e:
        logger.error(f"Error extracting node labels: {e}")
        traceback.print_exc()
        return []


def extract_unique_node_ids(meta_data_query: dict[str, Any]) -> dict[str, set[Any]] | list[Any]:
    """
    Extract all unique node_ids from meta_data_query.
    Works for both Attribute Search and Enumeration Search.

    Args:
        meta_data_query: API response dict containing meta_data_query

    Returns:
        Sorted list of unique node_ids

    Examples:
        ids = extract_unique_node_ids(api_response)
        # ['82aaec07-0278-4c24-82a5-fc5ac42692a3', '3c6259d5-3371-48a6-8337-450d30a0f989', ...]
    """
    try:
        logger.info("Extracting unique node IDs from meta_data_query...")
        query_result_node_ids = {}
        target_entities_unique_ids = set()
        related_entities_unique_ids = set()

        # Get meta_data_query
        # meta_data = api_results.get("meta_data_query", {})

        # Extract from target_entities
        target_entities = meta_data_query.get("target_entities", [])
        for target in target_entities:
            node_id = target.get("node_id")
            if node_id:
                target_entities_unique_ids.add(node_id)

        # Extract from related_entities
        related_entities = meta_data_query.get("related_entities", [])
        for related in related_entities:
            node_id = related.get("node_id")
            if node_id:
                related_entities_unique_ids.add(node_id)

        # Convert to sorted list
        # result = sorted(list(unique_ids))
        # logger.info(f" Found {len(result)} unique IDs: {result}")
        query_result_node_ids = {
            "node_ids": target_entities_unique_ids.union(related_entities_unique_ids),
        }
        return query_result_node_ids

    except Exception as e:
        logger.error(f"Error extracting node IDs: {e}")
        traceback.print_exc()
        return []


def extract_attribute_from_meta_data_query(meta_data_query: dict[str, Any]) -> list[Any]:
    attribute_result = []
    attributes_dict = meta_data_query.get("attributes", {})
    for attribute in attributes_dict:
        attribute_result.append(attribute.get("name"))

    return attribute_result


def prepare_reasoning_input(
    attribute_matches: AttributeMatchList | AttributeMatchMap,
    effective_from: Any | None = None,
    effective_to: Any | None = None,
    time: dict[str, Any] | None = None,
) -> ReasoningInputMap | TemporalReasoningInputMap:
    try:
        normalized_from, normalized_to = _resolve_reasoning_temporal_bounds(
            effective_from=effective_from,
            effective_to=effective_to,
            time=time,
        )

        # Result dict: node_id -> set of unique attribute keys
        input_data: dict[str, set[str]] = {}
        temporal_by_node: dict[str, tuple[str | None, str | None]] = {}

        # Format 1: List of attribute match dicts
        if isinstance(attribute_matches, list):
            for match in attribute_matches:
                if not isinstance(match, dict):
                    continue

                node_id = match.get("node_id")
                attribute_name = match.get("attribute_name")

                if not node_id or not attribute_name:
                    continue

                if node_id not in input_data:
                    input_data[node_id] = set()
                input_data[node_id].add(attribute_name)

                if normalized_from is not None or normalized_to is not None:
                    temporal_by_node[node_id] = (normalized_from, normalized_to)

        # Format 2: Dict mapping node_id -> {attr_key: attr_value, ...}
        elif isinstance(attribute_matches, dict):
            for node_id, attrs in attribute_matches.items():
                if not node_id or not isinstance(attrs, dict):
                    continue
                if node_id not in input_data:
                    input_data[node_id] = set()
                input_data[node_id].update(attrs.keys())

                attr_effective_from = normalized_from
                attr_effective_to = normalized_to
                if attr_effective_from is None and attr_effective_to is None:
                    attr_effective_from, attr_effective_to = _resolve_reasoning_temporal_bounds(
                        effective_from=attrs.get("effective_from"),
                        effective_to=attrs.get("effective_to"),
                    )

                if attr_effective_from is not None or attr_effective_to is not None:
                    temporal_by_node[node_id] = (attr_effective_from, attr_effective_to)

        # Convert sets to lists for output
        if temporal_by_node:
            result: TemporalReasoningInputMap = {
                node_id: {
                    "req_attrs": list(attrs),
                    "effective_from": temporal_by_node.get(node_id, (None, None))[0],
                    "effective_to": temporal_by_node.get(node_id, (None, None))[1],
                }
                for node_id, attrs in input_data.items()
            }
        else:
            result = {node_id: list(attrs) for node_id, attrs in input_data.items()}

        logger.info(f"Prepared {len(result)} nodes for reasoning")
        for node_id, attrs in result.items():
            logger.info(f"- {node_id}: {attrs}")

        return result

    except Exception as e:
        logger.error(f"Error preparing reasoning input: {e}")
        traceback.print_exc()
        return {}


def prepare_relation_enrich_input(
    graph_data: dict[str, Any],
    effective_from: Any | None = None,
    effective_to: Any | None = None,
    time: dict[str, Any] | None = None,
) -> RelationReasoningInput:
    """
    Transform graph query results into relation reasoning input_data.

    Each candidate inside graph_data maps to exactly one RelationEnrichItem
    inside the returned ``items`` list.

    Mapping rules
    -------------
    source_node_id        ← candidate["candidate_id"]
    relation_id           ← candidate["relationship_id"]
    relation_label        ← candidate["relationship_type"]
    destination_node_id   ← target_id  (the outer dict key)
    source_node_attributes← list of keys from candidate_properties,
                            excluding: source_documents, updated_at,
                            created_at, node_type, version, id
    relation_attributes   ← list of keys from relationship_properties,
                            excluding temporal bounds because they belong to
                            top-level reasoning input

    Args:
        graph_data: Dict keyed by target_id. Each value must contain at least
                    a ``"candidates"`` list with relationship metadata.
        effective_from: Optional query-level lower temporal bound.
        effective_to: Optional query-level upper temporal bound.
        time: Optional time dict with ``year`` or ``from_year``/``to_year``.

    Returns:
        RelationReasoningInput with ``items`` and optional top-level temporal
        bounds. Returns ``{"items": []}`` on error.

    Example input shape::

        {
            "<target_id>": {
                "target_id": "...",
                "candidates": [
                    {
                        "candidate_id": "...",
                        "candidate_properties": { ... },
                        "relationship_type": "APPLIES_TO",
                        "relationship_properties": { "quota": 80, ... },
                        "relationship_id": "5:...:784",
                    },
                    ...
                ],
            },
            ...
        }
    """
    try:
        normalized_from, normalized_to = _resolve_reasoning_temporal_bounds(
            effective_from=effective_from,
            effective_to=effective_to,
            time=time,
        )
        result_items: list[RelationEnrichItem] = []

        for target_id, target_data in graph_data.items():
            if not isinstance(target_data, dict):
                continue

            candidates = target_data.get("candidates", [])
            if not isinstance(candidates, list):
                continue

            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue

                candidate_id = candidate.get("candidate_id")
                relationship_id = candidate.get("relationship_id")
                relationship_type = candidate.get("relationship_type")
                candidate_properties = candidate.get("candidate_properties") or {}
                relationship_properties = candidate.get("relationship_properties") or {}

                # Skip malformed entries
                if not candidate_id or not relationship_id or not relationship_type:
                    logger.warning(
                        "Skipping candidate with missing required fields: %s",
                        candidate,
                    )
                    continue

                # Filter out non-analytical candidate properties
                filtered_source_attrs = [key for key in candidate_properties if key not in _CANDIDATE_PROPS_EXCLUDE]

                filtered_relation_attrs = [
                    key for key in relationship_properties.keys() if key not in _RELATION_PROPS_EXCLUDE
                ]

                item: RelationEnrichItem = {
                    "source_node_id": str(candidate_id),
                    "relation_id": str(relationship_id),
                    "relation_label": str(relationship_type),
                    "destination_node_id": target_id,
                    "source_node_attributes": filtered_source_attrs,
                    "relation_attributes": filtered_relation_attrs,
                }
                result_items.append(item)

        result: RelationReasoningInput = {"items": result_items}
        if normalized_from is not None or normalized_to is not None:
            result["effective_from"] = normalized_from
            result["effective_to"] = normalized_to

        logger.info(
            " prepare_relation_enrich_input: built %d RelationEnrichItem(s) from %d target(s).",
            len(result_items),
            len(graph_data),
        )
        return result

    except Exception as e:
        logger.error("Error in prepare_relation_enrich_input: %s", e)
        traceback.print_exc()
        return {"items": []}
