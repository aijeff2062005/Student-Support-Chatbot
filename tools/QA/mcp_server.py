"""MCP Server - Unified entry point for all database and utility operations.

This is the ONLY point of entry for all agents and services to access databases.
No agent should import database modules directly.

Architecture:
  All Agents/Services → MCP Server (HTTP RPC) → Service Layer → Databases
"""

import logging
import sys
import traceback
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field
from starlette.middleware.cors import CORSMiddleware

from configs.config_service import get_settings
from dbs.graph_search_helpers import (
    format_enumeration_search_result_with_targets,
    group_by_intermediate_nodes,
    sanitize_neo4j_types,
)
from dbs.keyword_search_milvus_helper import util
from dbs.milvus_helper import (
    get_nodes_by_type,
    search_entity_by_name,
    semantic_resolve_attributes,
)
from dbs.mongo_helper import fetch_media_for_entities
from utils.logging_config import configure_logging

settings = get_settings()

configure_logging()
logger = logging.getLogger(__name__)

# Initialize MCP server
mcp = FastMCP("EDU Unified Database Server", host=settings.mcp_host, port=settings.mcp_port)

# import all services (singleton instances)
try:
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))

    from .services.cache_service import get_cache_service

    # import local services
    from .services.embedding_service import get_embedding_service
    from .services.entity_service import get_entity_service
    from .services.milvus_service import get_milvus_service
    from .services.neo4j_service import get_neo4j_service

    logger.info(" All services imported successfully")
except ImportError as e:
    logger.error(f"Failed to import services: {e}")
    raise

embedding_service = get_embedding_service()
cache_service = get_cache_service()
entity_service = get_entity_service()
milvus_service = get_milvus_service()
neo4j_service = get_neo4j_service()

logger.info(" MCP Server initialized with all services")

# ============================================================================
# EMBEDDING TOOLS - Centralized embedding API access
# ============================================================================
logger = logging.getLogger(__name__)

# Initialize FastAPI app
app = FastAPI(
    title="EDU Unified Database Server",
    description="Unified API for embedding, cache, entity extraction, vector DB, and graph DB operations",
    version="1.0.0",
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class SearchVectorDBRequest(BaseModel):
    """Request model for vector database search"""

    target_entities: list[str] = Field(..., description="Target entities")
    top_k: int = Field(default=3, description="Number of results to return")
    threshold: float = Field(default=0.7, description="Minimum similarity threshold")


class AttributeSearchRequest(BaseModel):
    """Request model for attribute search"""

    target_entities: list[str] = Field(..., description="Target entities")
    related_entities_type: list[str] = Field([], description="Requested entity types")
    related_entities: list[str] = Field([], description="Requested related entities name")
    original_query: str = Field(..., description="Original user question")
    target_attribute: list[str] = Field(default=[], description="Attribute list of target_entities")
    related_entities_attribute: list[str] = Field(
        default=[],
        description="Attribute list of related_entities/related_entities_type",
    )
    is_required_media: bool = Field(default=False, description="Whether media content is required")
    effective_from: str | None = Field(
        None,
        description="Start of validity period (ISO UTC format: '2025-01-01T00:00:00Z')",
    )
    effective_to: str | None = Field(
        None,
        description="End of validity period (ISO UTC format: '2025-12-31T00:00:00Z')",
    )
    apply_temporal_to_intermediate: bool | None = Field(
        True,
        description="Whether to filter intermediate nodes in path by temporal constraints",
    )


class EnumerationSearchRequest(BaseModel):
    """Request model for enumeration search"""

    target_entities: list[str] = Field(..., description="Target entities")
    related_entities_type: list[str] = Field([], description="Requested entity types")
    related_entities: list[str] = Field([], description="Requested related entities")
    original_query: str = Field(..., description="Original user question")
    target_attribute: list[str] = Field(default=[], description="Attribute list of target_entities")
    related_entities_attribute: list[str] = Field(
        default=[],
        description="Attribute list of related_entities/related_entities_type",
    )
    is_required_media: bool = Field(default=False, description="Whether media content is required")
    effective_from: str | None = Field(
        None,
        description="Start of validity period (ISO UTC format: '2025-01-01T00:00:00Z')",
    )
    effective_to: str | None = Field(
        None,
        description="End of validity period (ISO UTC format: '2025-12-31T00:00:00Z')",
    )
    apply_temporal_to_intermediate: bool | None = Field(
        True,
        description="Whether to filter intermediate nodes in path by temporal constraints",
    )


class ComparisonSearchRequest(BaseModel):
    """Request model for comparison search"""

    target_entities: list[str] = Field(..., description="Target entities")
    related_entities_type: list[str] = Field(..., description="Requested entity types")
    original_query: str = Field(..., description="Original user question")
    requested_attributes: list[str] = Field(default=[], description="Requested attributes (optional)")


def clean_neo4j_data(data):
    """Recursively convert Neo4j protobuf objects to Python native types"""
    if data is None:
        return None

    # Handle protobuf list/tuple (RepeatedScalarContainer, etc.)
    if hasattr(data, "__iter__") and not isinstance(data, (str, dict, bytes)):
        return [clean_neo4j_data(item) for item in data]

    # Handle dict
    if isinstance(data, dict):
        return {key: clean_neo4j_data(value) for key, value in data.items()}

    # Handle primitive types
    return data


def filter_attribute_exists_for_node(
    requested_attributes: list[str], node_attributes: list[str], default_attribute=None
):
    if default_attribute is None:
        default_attribute = ["description"]

    node_attributes_set = set(node_attributes)  # lookup O(1)
    result = [attribute for attribute in requested_attributes if attribute in node_attributes_set]
    return result if result else default_attribute


def union_attribute_keys(target_results):
    return list(set().union(*(r["attribute_keys"] for r in target_results if r.get("attribute_keys"))))


def convert_year_to_neo4j(effective_from: str | None, effective_to: str | None) -> tuple[str | None, str | None]:
    """
    Convert year strings from LLM to Neo4j ISO datetime format.

    Args:
        effective_from: Year string from LLM (e.g., "2025" or "")
        effective_to: Year string from LLM (e.g., "2025" or "")

    Returns:
        Tuple[effective_from_iso, effective_to_iso] in format "YYYY-MM-DDTHH:MM:SSZ"
        Returns (None, None) if no temporal data

    Examples:
        >>> convert_year_to_neo4j("2025", "2025")
        ('2025-01-01T00:00:00Z', '2025-12-31T23:59:59Z')

        >>> convert_year_to_neo4j("2024", "2026")
        ('2024-01-01T00:00:00Z', '2026-12-31T23:59:59Z')

        >>> convert_year_to_neo4j("", "")
        (None, None)
    """

    # Check if empty strings or None
    if not effective_from and not effective_to:
        return None, None
    if effective_from:
        # Strip whitespace
        effective_from = effective_from.strip()
    if effective_to:
        effective_to = effective_to.strip()

    effective_from_iso = f"{effective_from}-01-01T00:00:00Z" if effective_from else None
    effective_to_iso = f"{effective_to}-12-31T23:59:59Z" if effective_to else None
    return effective_from_iso, effective_to_iso


def validate_year_strings(effective_from: str | None, effective_to: str | None) -> bool:
    """
    Validate year strings from LLM.

    Returns:
        True if valid, False otherwise
    """
    # Empty strings are valid (no temporal filtering)
    if not effective_from and not effective_to:
        return True

    if effective_from and not effective_to:
        return True

    if effective_to and not effective_from:
        return True

    # Both must be present or both empty
    if bool(effective_from) != bool(effective_to):
        return False

    if effective_from is None or effective_to is None:
        return False

    try:
        from_year = int(effective_from)
        to_year = int(effective_to)
        # to_year >= from_year
        if to_year < from_year:
            return False

        return True

    except (ValueError, TypeError):
        return False


def group_connections_by_target_and_type(
    direct_connections: list[dict] | dict[str, Any],
    indirect_connections: list[dict] | dict[str, Any],
    attributes_keys: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """
    Group connections by TARGET_ID first, then by NODE_TYPE.
    This preserves the relationship between target entities and their connections.

    Output structure:
    {
        "target_id_1": {
            "target_name": "...",
            "target_properties": {...},
            "NodeType1": {
                "direct": [...],
                "indirect": []
            },
            "NodeType2": {
                "direct": [...],
                "indirect": []
            }
        },
        "target_id_2": {...}
    }

    Args:
        direct_connections: Direct connections (dict format with target_id keys)
        indirect_connections: Indirect connections (dict format with target_id keys)
        attributes_keys: Optional list of attributes to extract

    Returns:
        Nested dict grouped by target_id, then by node_type
    """
    try:
        logger.info(" Grouping connections by target_id and node_type...")

        # Convert to dict format if needed
        direct_dict = direct_connections if isinstance(direct_connections, dict) else {}
        indirect_dict = indirect_connections if isinstance(indirect_connections, dict) else {}

        result = {}

        # Process each target_id
        all_target_ids = set(direct_dict.keys()) | set(indirect_dict.keys())

        for target_id in all_target_ids:
            logger.info(f"Processing target_id: {target_id}")

            # Initialize target structure
            target_data = {
                "target_id": target_id,
                "target_name": "",
                "target_description": "",
                "target_properties": {},
            }

            # Get direct connections for this target
            direct_target = direct_dict.get(target_id, {})
            indirect_target = indirect_dict.get(target_id, {})

            # Extract target metadata
            if direct_target:
                target_data["target_name"] = direct_target.get("target_name", "")
                target_data["target_description"] = direct_target.get("target_description", "")
                target_data["target_properties"] = direct_target.get("target_properties", {})
            elif indirect_target:
                target_data["target_name"] = indirect_target.get("target_name", "")
                target_data["target_description"] = indirect_target.get("target_description", "")
                target_data["target_properties"] = indirect_target.get("target_properties", {})

            # Get candidates
            direct_candidates = direct_target.get("candidates", [])
            indirect_candidates = indirect_target.get("candidates", [])

            # Group candidates by node_type
            node_types_data = {}

            # Process direct candidates
            for candidate in direct_candidates:
                candidate_props = candidate.get("candidate_properties", {})
                node_type = candidate_props.get("node_type") or candidate_props.get("type") or "Unknown"

                if node_type not in node_types_data:
                    node_types_data[node_type] = {"direct": [], "indirect": []}

                node_types_data[node_type]["direct"].append(candidate)

            # Process indirect candidates
            for candidate in indirect_candidates:
                candidate_props = candidate.get("candidate_properties", {})
                node_type = candidate_props.get("node_type") or candidate_props.get("type") or "Unknown"

                if node_type not in node_types_data:
                    node_types_data[node_type] = {"direct": [], "indirect": []}

                node_types_data[node_type]["indirect"].append(candidate)

            # Apply group_by_intermediate_nodes for each node_type
            for node_type, connections in node_types_data.items():
                logger.debug(
                    f"   {node_type}: {len(connections['direct'])} direct, {len(connections['indirect'])} indirect"
                )

                grouped_results = group_by_intermediate_nodes(
                    direct=connections["direct"],
                    indirect=connections["indirect"],
                    attribute_list=attributes_keys,
                )

                target_data[node_type] = grouped_results

            result[target_id] = target_data
            logger.info(f"Processed {len(node_types_data)} node types for target {target_id}")

        logger.info(f"Grouped into {len(result)} targets with node types")
        return sanitize_neo4j_types(result)

    except Exception as e:
        logger.error(f"Error grouping by target and type: {e}")
        traceback.print_exc()
        return {}


def build_graph_hop_mapping(
    direct_connections: list[dict] | dict[str, Any],  # ← Support both
    indirect_connections: list[dict] | dict[str, Any],  # ← Support both
) -> dict[str, int]:
    """
    Build mapping: candidate_id -> graph_hop.
    Supports both list and grouped dict formats.

    Args:
        direct_connections: List or {target_id: {candidates: [...]}}
        indirect_connections: List or {target_id: {candidates: [...]}}

    Returns:
        Dict mapping candidate_id to minimum graph_hop value
    """
    hop_mapping: dict[str, int] = {}

    def extract_candidates(data: list[dict] | dict[str, Any]) -> list[dict]:
        """Extract all candidates from any format."""
        if not data:
            return []

        # List format
        if isinstance(data, list):
            return data

        # Grouped dict format
        elif isinstance(data, dict):
            candidates = []
            for target_group in data.values():
                if isinstance(target_group, dict) and "candidates" in target_group:
                    candidates.extend(target_group.get("candidates", []))
            return candidates

        return []

    # Process DIRECT connections (hop=1 always)
    direct_candidates = extract_candidates(direct_connections)
    for candidate in direct_candidates:
        candidate_id = candidate.get("candidate_id")
        if candidate_id:
            hop_mapping[str(candidate_id)] = 1

    # Process INDIRECT connections (hop=graph_hop from path)
    indirect_candidates = extract_candidates(indirect_connections)
    for candidate in indirect_candidates:
        candidate_id = candidate.get("candidate_id")
        graph_hop = candidate.get("graph_hop", 20)

        if candidate_id:
            candidate_key = str(candidate_id)
            # Keep minimum hop if already exists
            if candidate_key not in hop_mapping:
                hop_mapping[candidate_key] = int(graph_hop)
            else:
                hop_mapping[candidate_key] = min(hop_mapping[candidate_key], int(graph_hop))

    logger.debug(f"Built hop mapping for {len(hop_mapping)} unique candidates")
    return hop_mapping


def build_meta_related_entities(
    relationship_results: dict[str, Any],
    entity_search_results: list[dict[str, Any]] | None = None,
    candidate_labels: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Build meta_data_query["related_entities"].
    Supports both list and grouped dict formats.

    Args:
        relationship_results: {
            "direct_connections": List or {target_id: {candidates: [...]}},
            "indirect_connections": List or {target_id: {candidates: [...]}}
        }
        entity_search_results: Milvus search results (for IDs mode)
        candidate_labels: Entity type labels (for Labels mode)

    Returns:
        List of related entity metadata with graph_hop
    """
    direct_connections = relationship_results.get("direct_connections", {})
    indirect_connections = relationship_results.get("indirect_connections", {})

    # Build candidate_id -> graph_hop mapping
    hop_mapping = build_graph_hop_mapping(direct_connections, indirect_connections)

    meta_entities = []

    # Helper to extract all candidates
    def get_all_candidates(data: list[dict] | dict[str, Any]) -> list[dict]:
        """Extract all candidates from any format."""
        if not data:
            return []

        # List format
        if isinstance(data, list):
            return data

        # Grouped dict format
        elif isinstance(data, dict):
            candidates = []
            for target_group in data.values():
                if isinstance(target_group, dict) and "candidates" in target_group:
                    candidates.extend(target_group.get("candidates", []))
            return candidates

        return []

    # MODE 1: Candidate IDs (specific entity search via Milvus)
    if entity_search_results:
        for entity in entity_search_results:
            node_id = entity.get("node_id")
            node_name = entity.get("node_name")
            node_type = entity.get("node_type")
            score = entity.get("score", 0.0)

            # Check if entity exists in graph
            node_key = str(node_id) if node_id is not None else ""
            graph_hop = hop_mapping.get(node_key, 20)
            is_verified = graph_hop < 20

            meta_entities.append(
                {
                    "query_value": node_name,
                    "node_id": node_id,
                    "confident_score_name": score,
                    "node_type": node_type,
                    "is_match_type": True,
                    "is_verified_by_graph": is_verified,
                    "graph_hop": graph_hop,
                }
            )

    # MODE 2: Candidate Labels (search by entity type)
    elif candidate_labels:
        # Extract unique candidates
        seen_nodes = set()

        all_candidates = get_all_candidates(direct_connections) + get_all_candidates(indirect_connections)

        for candidate in all_candidates:
            candidate_id = candidate.get("candidate_id")

            if candidate_id and candidate_id not in seen_nodes:
                seen_nodes.add(candidate_id)

                candidate_name = candidate.get("candidate_name", "")
                candidate_props = candidate.get("candidate_properties", {})
                node_type = candidate_props.get("node_type")

                # Get hop from mapping
                graph_hop = hop_mapping.get(str(candidate_id), 20)

                meta_entities.append(
                    {
                        "query_value": candidate_name,
                        "node_id": candidate_id,
                        "node_type": node_type,
                        "confident_score_type": 1,
                        "is_match_type": True,
                        "is_verified_by_graph": True,
                        "graph_hop": graph_hop,
                    }
                )

    logger.debug(f"Built metadata for {len(meta_entities)} related entities")
    return meta_entities


# @mcp.tool(description="Search vector database (Milvus) for simple concepts")
@app.post("/search_vector_db")
def search_vector_db(request: SearchVectorDBRequest) -> dict[Any, Any]:
    """Search Milvus vector database for similar documents.

    This is the ONLY way to search the vector database.

    Args:
        request

    Returns:
        Search results from vector database
    """
    try:
        targets_entities = request.target_entities
        targets_results = search_entity_by_name(targets_entities, top_k=request.top_k, threshold=request.threshold)

        logger.info(f"Found {len(targets_results)} results for {len(targets_entities)} entities")
        meta_data_query = {
            "target_entities": [
                {
                    "query_value": n["node_name"],
                    "confident_score_name": n["score"],
                    "node_id": n["node_id"],
                    "node_type": n["node_type"],
                }
                for n in targets_results
            ]
        }

        #  Clean protobuf objects before return
        import json

        # attribute_keys = targets_results[0].get("attribute_keys")
        clean_results = json.loads(json.dumps(targets_results, default=str))
        logger.info(f"meta_data_query: {meta_data_query}")
        return {
            "status": "success",
            "results": clean_results,
            "meta_data_query": meta_data_query,
            "meta_input_query": {
                "target_entities": request.target_entities,
            },
            "count": len(clean_results),
        }

    except Exception as e:
        logger.error(f"Error in search_vector_db: {e}")
        traceback.print_exc()
        return {"status": "error", "results": [], "count": 0, "error": str(e)}


# @mcp.tool(description="Search for attributes or related entities (NEW IMPLEMENTATION)")
@app.post("/attribute_search")
async def attribute_search(request: AttributeSearchRequest) -> dict[str, Any]:
    """
    Attribute search - Find specific attributes or related entities.
    NOW WITH MEDIA SUPPORT, SPECIFIC ENTITY SEARCH, AND TEMPORAL FILTERING!
    """
    try:
        meta_data_query = {}
        target_entities = request.target_entities
        requested_entity_type = request.related_entities_type
        requested_entities = request.related_entities
        target_attribute = request.target_attribute if request.target_attribute else []
        related_entities_attribute = request.related_entities_attribute if request.related_entities_attribute else []
        original_query = request.original_query
        is_required_media = request.is_required_media if request.is_required_media else False
        logger.info(" ATTRIBUTE SEARCH started")
        logger.info(f"target: {request.target_entities}")
        logger.info(f"requested_type: {request.related_entities_type}")
        logger.info(f"requested_entities: {request.related_entities}")
        logger.info(f"media_required: {request.is_required_media}")
        effective_from_year = getattr(request, "effective_from", "")
        effective_to_year = getattr(request, "effective_to", "")

        effective_from_iso = None
        effective_to_iso = None

        if effective_from_year or effective_to_year:
            target_attribute.extend(["effective_from", "effective_to"])
            related_entities_attribute.extend(["effective_from", "effective_to"])
            logger.info(f"Temporal from LLM: {effective_from_year}  {effective_to_year}")

            # Validate
            if not validate_year_strings(effective_from_year, effective_to_year):
                logger.warning(f"Invalid year strings: {effective_from_year}, {effective_to_year}")
            else:
                # Convert to Neo4j format
                effective_from_iso, effective_to_iso = convert_year_to_neo4j(effective_from_year, effective_to_year)

                if effective_from_iso or effective_to_iso:
                    logger.info("Converted to Neo4j format:")
                    logger.info(f"effective_from: {effective_from_iso}")
                    logger.info(f"effective_to: {effective_to_iso}")

        # Find target node IDs
        target_results = search_entity_by_name(entity_name=target_entities, top_k=1)
        # logger.error(f"target_entities: {target_results}")
        if not target_results:
            target_results = []
            keyword_results = util.get_best(question=original_query)
            if keyword_results:
                for keyword_result in keyword_results.record:
                    # logger.error(f"keyword_results: {keyword_results}")
                    node_id = keyword_result.metadata.get("node_id")
                    target_result = {
                        "node_id": node_id,
                        "attribute_keys": [keyword_result.metadata.get("attribute")],
                        "score": keyword_result.score,  # Use keyword search score
                    }
                    raw_attributes = target_result["attribute_keys"]
                    target_attribute.extend([attr for attr in raw_attributes if isinstance(attr, str)])
                    if not isinstance(node_id, str) or not node_id:
                        continue
                    node_properties = neo4j_service.get_node_attribute(
                        node_ids=[node_id], attributes_keys=["labels", "name"]
                    )
                    logger.info(f"node_properties: {node_properties}")
                    node_property = node_properties.get(node_id) if isinstance(node_properties, dict) else None
                    target_result.update(
                        {
                            "node_name": node_property.get("name") if isinstance(node_property, dict) else None,
                            "node_type": node_property.get("labels") if isinstance(node_property, dict) else None,
                        }
                    )
                    target_results.append(target_result)
            else:
                return {
                    "status": "not_found",
                    "message": f"Entity not found: {target_entities[0]}",
                }
        logger.info(f"target_results: {target_results}")

        target_node_attributes = union_attribute_keys(target_results=target_results)
        target_attribute_results = await semantic_resolve_attributes(
            requested_attributes=target_attribute,
            available_attribute_keys=target_node_attributes,
        )
        logger.info(f"attribute_results: {target_attribute_results}")
        # if is_fallback:
        #     target_results = []
        #     logger.error(f"Keyword search when attribute not matching")
        #     keyword_target_results = search_entity_by_name(entity_name=target_entities, top_k=40, threshold=0.6)
        #     keyword_target_node_ids = [n["node_id"] for n in keyword_target_results]
        #     # text_cond = f'TEXT_MATCH(content, )'
        #     if keyword_target_node_ids:
        #         if len(keyword_target_node_ids) == 1:
        #             expr = f'node_id == "{keyword_target_node_ids[0]}"'
        #         else:
        #             import json
        #             expr = f'node_id in {json.dumps(keyword_target_node_ids)}'
        #         keyword_results = util.get_best(question=original_query, filters=expr)
        #         # keyword_results = util.get_best(question=original_query)
        #         if keyword_results:
        #             for keyword_result in keyword_results.record:
        #                 # logger.error(f"keyword_results: {keyword_results}")
        #                 node_id = keyword_result.metadata.get("node_id")
        #                 node = next(
        #                     (item for item in keyword_target_results if item.get("node_id") == node_id),
        #                     None
        #                 )
        #                 logger.error(f"Keyword search: {node}")
        #                 new_node_attributes = [keyword_result.metadata.get("attribute")]
        #                 logger.error(f'new_node_attributes: {new_node_attributes}')
        #                 # requested_attributes.extend(attribute for attribute in new_node_attributes)
        #                 node_properties = neo4j_service.get_node_attribute(
        #                     node_ids=[node_id],
        #                     attributes_keys=["labels", "name"]
        #                 )
        #                 logger.error("node_properties: {}".format(node_properties))
        #                 # new_node_attributes = union_attribute_keys(target_results=target_results)
        #                 keyword_attributes = union_attribute_keys(target_results=[node])
        #                 logger.error("keyword_attributes: {}".format(keyword_attributes))
        #                 keyword_attribute_results = await semantic_resolve_attributes(
        #                     requested_attributes=requested_attributes,
        #                     available_attribute_keys=keyword_attributes
        #                 )
        #                 attribute_results.extend(keyword_attribute_results)
        #                 target_results.append(node)
        #
        #
        # logger.error(f"attribute_results after callback: {attribute_results}")
        target_node_ids = [n["node_id"] for n in target_results]
        target_attributes_keys = [result["attribute_key"] for result in target_attribute_results]
        logger.info(f"target_attributes_keys: {target_attributes_keys}")
        target_node_ids = list(dict.fromkeys(target_node_ids))

        # Build meta for target entities
        unique_targets: dict[str, dict] = {}

        for n in target_results:
            node_id = n["node_id"]

            if node_id not in unique_targets:
                unique_targets[node_id] = n
            else:
                if n["score"] > unique_targets[node_id]["score"]:
                    unique_targets[node_id] = n

        meta_data_query["target_entities"] = [
            {
                "query_value": n["node_name"],
                "confident_score_name": n["score"],
                "node_id": n["node_id"],
                "node_type": n["node_type"],
            }
            for n in unique_targets.values()
        ]

        # Build meta for attributes
        meta_data_query.setdefault("attributes", {})

        meta_data_query["attributes"]["target_attribute"] = [
            {"name": n["attribute_key"], "confidence_score_attribute": n["score"]} for n in target_attribute_results
        ]

        logger.info(f"Found target node: {target_node_ids}")

        target_attribute_value = neo4j_service.get_node_attribute(
            node_ids=target_node_ids, attributes_keys=target_attributes_keys
        )

        results = None
        entity_search_results = []

        # BRANCH A: Has requested_entity_type
        if (requested_entity_type or requested_entities) and related_entities_attribute:
            logger.info(f"Branch A: Finding related entities: {requested_entity_type or requested_entities}")

            # Determine search mode
            candidate_node_ids = None
            candidate_labels = requested_entity_type  # Default to labels mode
            # Get attributes for candidate types
            candidate_attributes = get_nodes_by_type(node_types=requested_entity_type)
            # Check if specific entities mentioned
            if requested_entities:
                logger.info(f"Searching for specific entities: {requested_entities}")

                # Search Milvus for specific entity names
                entity_search_results = search_entity_by_name(
                    entity_name=requested_entities,
                    entity_type=requested_entity_type,
                    threshold=0.7,
                    top_k=50,
                )
                candidate_attributes_results = union_attribute_keys(entity_search_results)
                logger.info(f"candidate_attributes_results: {candidate_attributes_results}")

                related_attribute_results = await semantic_resolve_attributes(
                    requested_attributes=related_entities_attribute,
                    available_attribute_keys=candidate_attributes_results,
                )
                logger.info(f"related_attribute_results: {related_attribute_results}")

                related_attributes_keys = [result["attribute_key"] for result in related_attribute_results]
                meta_data_query["attributes"]["related_entities_attribute"] = [
                    {
                        "name": n["attribute_key"],
                        "confidence_score_attribute": n["score"],
                    }
                    for n in related_attribute_results
                ]
                # Extract candidate IDs
                candidate_node_ids = [r["node_id"] for r in entity_search_results]

                if candidate_node_ids:
                    logger.info(f"Found {len(candidate_node_ids)} specific entities (IDs mode)")
                    candidate_labels = None  # Use IDs mode
                else:
                    logger.warning("No specific entities found, falling back to labels mode")
            else:
                candidate_attributes_results = union_attribute_keys(candidate_attributes)
                logger.info(f"candidate_attributes_results: {candidate_attributes_results}")

                related_attribute_results = await semantic_resolve_attributes(
                    requested_attributes=related_entities_attribute,
                    available_attribute_keys=candidate_attributes_results,
                )
                logger.info(f"related_attribute_results: {related_attribute_results}")

                related_attributes_keys = [result["attribute_key"] for result in related_attribute_results]
                meta_data_query["attributes"]["related_entities_attribute"] = [
                    {
                        "name": n["attribute_key"],
                        "confidence_score_attribute": n["score"],
                    }
                    for n in related_attribute_results
                ]
            #  Find relationships WITH TEMPORAL FILTERING
            relationship_results = neo4j_service.find_node_relationship(
                target_node_ids=target_node_ids,
                candidate_node_ids=candidate_node_ids,
                candidate_labels=candidate_labels,
                max_depth=3,
                include_intermediate=True,
                effective_from=effective_from_year,  #  Auto-converted to ISO inside
                effective_to=effective_to_year,  #  Auto-converted to ISO inside
                apply_temporal_to_intermediate=getattr(request, "apply_temporal_to_intermediate", True),
            )

            # BUILD META RELATED ENTITIES WITH PROPER GRAPH_HOP
            meta_data_query["related_entities"] = build_meta_related_entities(
                relationship_results=relationship_results,
                entity_search_results=entity_search_results if candidate_node_ids else [],
                candidate_labels=candidate_labels if not candidate_node_ids else [],
            )

            # Combine direct and indirect connections
            direct_connections = relationship_results["direct_connections"]
            indirect_connections = relationship_results["indirect_connections"]
            all_grouped_results = group_connections_by_target_and_type(
                direct_connections=direct_connections,
                indirect_connections=indirect_connections,
                attributes_keys=related_attributes_keys,
            )

            results = {
                "connected_entities": sanitize_neo4j_types(all_grouped_results),
                "target_attribute_value": sanitize_neo4j_types(target_attribute_value),
            }
        else:
            # BRANCH B: No requested_entity_type
            results = sanitize_neo4j_types(target_attribute_value)

        # ========================================
        # FETCH MEDIA IF REQUIRED
        # ========================================
        media_data = None

        if is_required_media and target_node_ids:
            try:
                logger.info(f"Fetching media for {len(target_node_ids)} nodes...")

                media_response = fetch_media_for_entities(
                    node_ids=target_node_ids,
                )

                if media_response and media_response.get("attachments"):
                    media_data = media_response
                    logger.info(f"Fetched {len(media_data['attachments'])} media items")
                else:
                    logger.warning("No media found for target entities")

            except Exception as media_error:
                logger.error(f"Media fetch failed: {media_error}")
                media_data = None

        # ========================================
        # BUILD RESPONSE
        # ========================================
        logger.info(f"meta_data_query: {meta_data_query}")

        response = {
            "status": "success",
            "results": results,
            "meta_data_query": meta_data_query,
            "meta_input_query": {
                "target_entities": request.target_entities,
                "related_entities_type": request.related_entities_type,
                "related_entities": request.related_entities,
                "target_attribute": request.target_attribute,
                "related_entities_attribute": request.related_entities_attribute,
            },
        }
        # logger.error(f"is_fallback_target: {is_fallback_target} and is_fallback_related {is_fallback_related}")

        if media_data and media_data.get("attachments"):
            response["media"] = media_data

        return response

    except Exception as e:
        traceback.print_exc()
        logger.error(f"Attribute search error: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}


@app.post("/enumeration_search")
async def enumeration_search(request: EnumerationSearchRequest) -> dict[str, Any]:
    """
    Enumeration search - List multiple entities or attributes.
    NOW WITH MEDIA SUPPORT, SPECIFIC ENTITY SEARCH, AND TEMPORAL FILTERING!
    """
    try:
        meta_data_query = {}
        target_entities = request.target_entities
        requested_entity_type = request.related_entities_type
        requested_entities = request.related_entities
        target_attribute = request.target_attribute if request.target_attribute else []
        related_entities_attribute = request.related_entities_attribute if request.related_entities_attribute else []
        original_query = request.original_query
        is_required_media = request.is_required_media if request.is_required_media else False
        logger.info("ENUMERATION SEARCH started")
        logger.info(f"target: {request.target_entities}")
        logger.info(f"requested_type: {request.related_entities_type}")
        logger.info(f"requested_entities: {request.related_entities}")
        logger.info(f"media_required: {request.is_required_media}")

        effective_from_year = getattr(request, "effective_from", "")
        effective_to_year = getattr(request, "effective_to", "")

        effective_from_iso = None
        effective_to_iso = None

        if effective_from_year or effective_to_year:
            target_attribute.extend(["effective_from", "effective_to"])
            related_entities_attribute.extend(["effective_from", "effective_to"])
            logger.info(f"Temporal from LLM: {effective_from_year}  {effective_to_year}")

            # Validate
            if not validate_year_strings(effective_from_year, effective_to_year):
                logger.warning(f"Invalid year strings: {effective_from_year}, {effective_to_year}")
            else:
                # Convert to Neo4j format
                effective_from_iso, effective_to_iso = convert_year_to_neo4j(effective_from_year, effective_to_year)

                if effective_from_iso and effective_to_iso:
                    logger.info("Converted to Neo4j format:")
                    logger.info(f"effective_from: {effective_from_iso}")
                    logger.info(f"effective_to: {effective_to_iso}")

        # ========================================
        # PHASE 1: Find target_entities node_id via Milvus
        # ========================================
        target_results = search_entity_by_name(entity_name=target_entities, top_k=1)
        if not target_results:
            target_results = []
            keyword_results = util.get_best(question=original_query)
            if keyword_results:
                for keyword_result in keyword_results.record:
                    # logger.error(f"keyword_results: {keyword_results}")
                    node_id = keyword_result.metadata.get("node_id")
                    target_result = {
                        "node_id": node_id,
                        "attribute_keys": [
                            keyword_result.metadata.get("attribute"),
                            "name",
                            "description",
                            "code",
                        ],
                        "score": keyword_result.score,  # Use keyword search score
                    }
                    raw_attributes = target_result["attribute_keys"]
                    target_attribute.extend([attr for attr in raw_attributes if isinstance(attr, str)])
                    if not isinstance(node_id, str) or not node_id:
                        continue
                    node_properties = neo4j_service.get_node_attribute(
                        node_ids=[node_id], attributes_keys=["labels", "name"]
                    )
                    logger.info(f"node_properties: {node_properties}")
                    node_property = node_properties.get(node_id) if isinstance(node_properties, dict) else None
                    target_result.update(
                        {
                            "node_name": node_property.get("name") if isinstance(node_property, dict) else None,
                            "node_type": node_property.get("labels") if isinstance(node_property, dict) else None,
                        }
                    )
                    target_results.append(target_result)
            else:
                return {
                    "status": "not_found",
                    "message": f"Entity not found: {target_entities[0]}",
                }
        logger.info(f"target_results: {target_results}")
        target_node_attributes = union_attribute_keys(target_results=target_results)
        target_attribute_results = await semantic_resolve_attributes(
            requested_attributes=target_attribute,
            available_attribute_keys=target_node_attributes,
        )
        target_node_ids = [n["node_id"] for n in target_results]
        target_attributes_keys = [result["attribute_key"] for result in target_attribute_results]
        logger.info(f"target_attributes_keys: {target_attributes_keys}")
        target_attribute_value = neo4j_service.get_node_attribute(
            node_ids=target_node_ids, attributes_keys=target_attributes_keys
        )
        target_node_ids = list(dict.fromkeys(target_node_ids))
        unique_targets: dict[str, dict] = {}

        for n in target_results:
            node_id = n["node_id"]

            if node_id not in unique_targets:
                unique_targets[node_id] = n
            else:
                if n["score"] > unique_targets[node_id]["score"]:
                    unique_targets[node_id] = n

        meta_data_query["target_entities"] = [
            {
                "query_value": n["node_name"],
                "confident_score_name": n["score"],
                "node_id": n["node_id"],
                "node_type": n["node_type"],
            }
            for n in unique_targets.values()
        ]

        # Build meta for attributes
        meta_data_query.setdefault("attributes", {})

        meta_data_query["attributes"]["target_attribute"] = [
            {"name": n["attribute_key"], "confidence_score_attribute": n["score"]} for n in target_attribute_results
        ]

        final_results = None

        # ========================================
        # BRANCH A: Has requested_entity_type
        # ========================================
        if requested_entity_type and len(requested_entity_type) > 0:
            logger.info(f"Branch A: Enumerating entities of types: {requested_entity_type}")

            # Determine search mode
            candidate_node_ids = None
            candidate_labels = requested_entity_type  # Default to labels mode

            # Get attributes for candidate types
            candidate_attributes = get_nodes_by_type(node_types=requested_entity_type)

            entity_search_results = []

            # ========================================
            # Check if specific entities mentioned
            # ========================================
            if requested_entities and len(requested_entities) > 0:
                logger.info(f"Searching for specific entities: {requested_entities}")

                # Search Milvus for specific entity names
                entity_search_results = search_entity_by_name(
                    entity_name=requested_entities,
                    entity_type=requested_entity_type,
                    threshold=0.7,
                    top_k=20,
                )
                candidate_attributes_results = union_attribute_keys(entity_search_results)

                related_attribute_results = await semantic_resolve_attributes(
                    requested_attributes=related_entities_attribute,
                    available_attribute_keys=candidate_attributes_results,
                )
                logger.info(f"related_attribute_results: {related_attribute_results}")

                related_attributes_keys = [result["attribute_key"] for result in related_attribute_results]
                meta_data_query["attributes"]["related_entities_attribute"] = [
                    {
                        "name": n["attribute_key"],
                        "confidence_score_attribute": n["score"],
                    }
                    for n in related_attribute_results
                ]
                # Extract candidate IDs
                candidate_node_ids = [r["node_id"] for r in entity_search_results]

                if candidate_node_ids:
                    logger.info(f"Found {len(candidate_node_ids)} specific entities (IDs mode)")
                    candidate_labels = None  # Use IDs mode
                else:
                    logger.warning(" No specific entities found, falling back to labels mode")
            else:
                candidate_attributes_results = union_attribute_keys(candidate_attributes)

                related_attribute_results = await semantic_resolve_attributes(
                    requested_attributes=related_entities_attribute,
                    available_attribute_keys=candidate_attributes_results,
                )
                logger.info(f"related_attribute_results: {related_attribute_results}")

                related_attributes_keys = [result["attribute_key"] for result in related_attribute_results]
                meta_data_query["attributes"]["related_entities_attribute"] = [
                    {
                        "name": n["attribute_key"],
                        "confidence_score_attribute": n["score"],
                    }
                    for n in related_attribute_results
                ]

            # ========================================
            #  Find relationships WITH TEMPORAL FILTERING
            # ========================================
            relationship_results = neo4j_service.find_node_relationship(
                target_node_ids=target_node_ids,
                candidate_node_ids=candidate_node_ids,
                candidate_labels=candidate_labels,
                max_depth=5,
                include_intermediate=True,
                effective_from=effective_from_year,  #  Auto-converted to ISO inside
                effective_to=effective_to_year,  #  Auto-converted to ISO inside
                apply_temporal_to_intermediate=getattr(request, "apply_temporal_to_intermediate", True),
            )

            direct_connections = relationship_results["direct_connections"]
            indirect_connections = relationship_results["indirect_connections"]

            # ========================================
            # Check if any connections found
            # ========================================
            def count_connections(conns):
                if isinstance(conns, list):
                    return len(conns)
                elif isinstance(conns, dict):
                    return sum(len(g.get("candidates", [])) for g in conns.values())
                return 0

            total_connections = count_connections(direct_connections) + count_connections(indirect_connections)

            if total_connections == 0:
                #  BUILD META RELATED ENTITIES (empty case)
                meta_data_query["related_entities"] = build_meta_related_entities(
                    relationship_results=relationship_results,
                    entity_search_results=entity_search_results if candidate_node_ids else [],
                    candidate_labels=candidate_labels if not candidate_node_ids else [],
                )

                return {
                    "status": "success",
                    "message": f"No relationships found for {target_entities} and {requested_entities if requested_entities else requested_entity_type}",
                    "meta_data_query": meta_data_query,
                }

            logger.info(f"Found {total_connections} total connections")

            # ========================================
            # BUILD META RELATED ENTITIES WITH PROPER GRAPH_HOP
            # ========================================
            meta_data_query["related_entities"] = build_meta_related_entities(
                relationship_results=relationship_results,
                entity_search_results=entity_search_results if candidate_node_ids else [],
                candidate_labels=candidate_labels if not candidate_node_ids else [],
            )

            # ========================================
            # GROUP BY NODE TYPE
            # ========================================
            all_grouped_results = group_connections_by_target_and_type(
                direct_connections=direct_connections,
                indirect_connections=indirect_connections,
                attributes_keys=related_attributes_keys,
            )

            # ========================================
            # FORMAT RESULTS
            # ========================================
            final_results = format_enumeration_search_result_with_targets(
                search_type="enumeration_entities",
                data=all_grouped_results if related_attributes_keys else sanitize_neo4j_types(all_grouped_results),
            )

        # ========================================
        # MEDIA FETCHING (if required)
        # ========================================
        media_data = None

        if is_required_media and target_node_ids:
            try:
                logger.info(f" Fetching media for {len(target_node_ids)} nodes...")

                media_response = fetch_media_for_entities(
                    node_ids=target_node_ids,
                )

                if media_response and media_response.get("attachments"):
                    media_data = media_response
                    logger.info(f"Fetched {len(media_data['attachments'])} media items")
                else:
                    logger.warning(" No media found for target entities")

            except Exception as media_error:
                logger.error(f"Media fetch failed: {media_error}")
                media_data = None

        # ========================================
        # BUILD RESPONSE
        # ========================================
        if isinstance(final_results, dict):
            response = {
                "final_results": sanitize_neo4j_types(final_results),
                "target_attribute_value": sanitize_neo4j_types(target_attribute_value),
            }
        else:
            # final_results["target_attribute_value"] = target_attribute_value
            response = {
                "status": "success",
                "results": {
                    "final_results": sanitize_neo4j_types(final_results),
                    "target_attribute_value": sanitize_neo4j_types(target_attribute_value),
                },
            }

        if media_data and media_data.get("attachments"):
            response["media"] = media_data

        response["meta_data_query"] = meta_data_query
        response["meta_input_query"] = {
            "target_entities": request.target_entities,
            "related_entities_type": request.related_entities_type,
            "related_entities": request.related_entities,
            "target_attribute": request.target_attribute,
            "related_entities_attribute": request.related_entities_attribute,
        }
        logger.info(" Enumeration search completed successfully")
        return response

    except Exception as e:
        logger.error(f"Enumeration search error: {e}", exc_info=True)
        traceback.print_exc()
        return {"status": "error", "message": str(e)}


# @mcp.tool(description="Compare multiple entities by retrieving all their properties")
@app.post("/comparison_search")
def comparison_search(
    request: ComparisonSearchRequest,
) -> dict[str, Any]:  #  Return Dict, not List
    """Comparison search for multiple entities."""
    try:
        logger.info(" Comparison search started")

        entities = request.target_entities
        related_entities_type = request.related_entities_type if request.related_entities_type else None

        logger.info(f"Comparing: {entities}")

        node_ids = []
        node_types = []
        entity_names = []

        milvus_result = search_entity_by_name(entities)
        if milvus_result:
            for result in milvus_result:
                node_ids.append(result.get("node_id"))
                node_types.append(result.get("node_type"))
                entity_names.append(result)
        else:
            logger.warning(f"Entity not found: {entities}")

        if not node_ids:
            return {
                "status": "not_found",
                "message": "No entities found in knowledge base",
                "results": [],
            }

        results = neo4j_service.comparison_search_neo4j(
            node_ids=node_ids,
            node_types=node_types,
            attribute_list=related_entities_type,
        )

        # Clean everything
        cleaned_results = clean_neo4j_data(results)
        cleaned_entity_names = clean_neo4j_data(entity_names)

        # Add entity names
        for i, result in enumerate(cleaned_results):
            if i < len(cleaned_entity_names):
                result["entity_name"] = cleaned_entity_names[i]

        logger.info(f"Comparison complete: {len(cleaned_results)} entities")

        #  Return wrapped in dict (consistent with other APIs)
        import json

        final_results = json.loads(json.dumps(cleaned_results, default=str))

        # Build meta_data_query
        meta_data_query = {
            "target_entities": [
                {
                    "query_value": n.get("node_name"),
                    "confident_score_name": n.get("score"),
                    "node_id": n.get("node_id"),
                    "node_type": n.get("node_type"),
                }
                for n in milvus_result
            ]
        }

        # Build meta_input_query
        meta_input_query = {
            "target_entities": request.target_entities,
            "related_entities_type": request.related_entities_type,
            "original_query": request.original_query,
            "requested_attributes": request.requested_attributes,
        }
        logger.info(f"comparison_search API meta_data_query: {meta_data_query}")

        return {
            "status": "success",
            "count": len(final_results),
            "results": final_results,
            "meta_data_query": meta_data_query,
            "meta_input_query": meta_input_query,
        }

    except Exception as e:
        logger.error(f"Comparison search error: {e}", exc_info=True)
        return {"status": "error", "message": str(e), "results": []}


# Export ASGI app for uvicorn
# app = mcp.streamable_http_app()


if __name__ == "__main__":
    import uvicorn

    logger.info("=" * 70)
    logger.info(" EDU MCP Server Starting")
    logger.info("Embedding | Cache | Entity Extraction | Vector DB | Graph DB | Utility")
    logger.info(" Listening on http://0.0.0.0:%s", settings.mcp_port)
    logger.info("=" * 70)

    uvicorn.run(app, host="0.0.0.0", port=settings.mcp_port)
