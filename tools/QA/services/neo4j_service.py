"""Neo4j Graph Database Service.

Wrapper around neo4j_helper to provide clean abstraction layer.
Delegates to neo4j_helper.search_graph_with_vector() for LightRAG-based search.
"""

import logging
import traceback
from typing import Any, Optional, cast

from neo4j import GraphDatabase

from configs.config_service import get_settings
from dbs.graph_search_helpers import _normalize_temporal_params, sanitize_neo4j_types
from dbs.milvus_helper import search_entity_by_name

from .embedding_service import get_embedding_service

logger = logging.getLogger(__name__)


def build_temporal_where_clause(
    node_alias: str, effective_from: str | None = None, effective_to: str | None = None
) -> str:
    """
    Build Neo4j WHERE clause for temporal validity filtering.

    Two intervals overlap iff:  start1 <= end2  AND  end1 >= start2
    When one boundary is NULL, treat that side as open-ended (always valid).

    Cases handled (entity = node or relationship):
      1. Entity has NO temporal fields → always valid
      2. Entity has only effective_from → still valid if it started before query ends
      3. Entity has only effective_to   → still valid if it ends after query starts
      4. Entity has both → standard interval overlap check

    Uses toString() for comparisons instead of datetime() to handle MIXED
    temporal types in Neo4j (LOCAL_DATE_TIME, DATE_TIME, STRING).
    Neo4j returns NULL when comparing LOCAL_DATE_TIME with ZONED_DATE_TIME,
    which causes ALL(rel IN relationships(path) WHERE ...) to fail silently.
    toString() converts any temporal type to ISO string, and lexicographic
    comparison works correctly for ISO 8601 format.

    Works for both node aliases (e.g. 'candidate') and relationship aliases
    (e.g. 'r') — Cypher property access syntax is the same.

    Args:
        node_alias: Variable name in Cypher (node or relationship, e.g., 'r', 'candidate')
        effective_from: Query start time (ISO UTC format)
        effective_to: Query end time (ISO UTC format)

    Returns:
        Cypher WHERE clause string (empty if no temporal filtering needed)
    """

    if not effective_from and not effective_to:
        return ""

    a = node_alias  # shorthand

    conditions = [f"({a}.effective_from IS NULL AND {a}.effective_to IS NULL)"]

    if effective_from and effective_to:
        # Case 1, 2, 3, 4, 5 (Happy case data):
        conditions.append(
            f"({a}.effective_from IS NOT NULL AND {a}.effective_to IS NOT NULL "
            f"AND datetime('{effective_from}') >= datetime({a}.effective_from) "
            f"AND datetime('{effective_to}') <= datetime({a}.effective_to))"
        )

        # Case 1, 2, 3 (data just have effective_from):
        conditions.append(
            f"({a}.effective_from IS NOT NULL AND {a}.effective_to IS NULL "
            f"AND datetime('{effective_from}') >= datetime({a}.effective_from))"
        )

        # Case 1, 2, 3 (data just have effective_to):
        conditions.append(
            f"({a}.effective_from IS NULL AND {a}.effective_to IS NOT NULL "
            f"AND datetime('{effective_to}') <= datetime({a}.effective_to))"
        )

    elif effective_from:
        # Case 1, 2, 3 (Happy case data):
        conditions.append(
            f"({a}.effective_from IS NOT NULL AND {a}.effective_to IS NOT NULL "
            f"AND datetime('{effective_from}') <= datetime({a}.effective_to))"
        )

        # Case 1, 2 (data just have effective_from):
        conditions.append(
            f"({a}.effective_from IS NOT NULL AND {a}.effective_to IS NULL "
            f"AND datetime('{effective_from}') <= datetime({a}.effective_from))"
        )

        # Case 1, 2 (data just have effective_to):
        conditions.append(
            f"({a}.effective_from IS NULL AND {a}.effective_to IS NOT NULL "
            f"AND datetime('{effective_from}') <= datetime({a}.effective_to))"
        )

    elif effective_to:
        # Case 1, 2, 3 (Happy case data):
        conditions.append(
            f"({a}.effective_from IS NOT NULL AND {a}.effective_to IS NOT NULL "
            f"AND datetime('{effective_to}') >= datetime({a}.effective_from)"
            f"AND datetime('{effective_to}') <= datetime({a}.effective_to))"
        )

        # Case 1, 2 (data just have effective_from):
        conditions.append(
            f"({a}.effective_from IS NOT NULL AND {a}.effective_to IS NULL "
            f"AND datetime('{effective_to}') >= datetime({a}.effective_from))"
        )

        # Case 1, 2 (data just have effective_to):
        conditions.append(
            f"({a}.effective_from IS NULL AND {a}.effective_to IS NOT NULL "
            f"AND datetime('{effective_to}') <= datetime({a}.effective_to))"
        )

    return "(" + " OR ".join(conditions) + ")"


def check_temporal_overlap(
    entity_from: str | None,
    entity_to: str | None,
    query_from: str | None,
    query_to: str | None,
) -> bool:
    """
    Pure-Python companion to build_temporal_where_clause.
    Same interval-overlap logic, usable for post-processing data already in memory
    (e.g. Milvus relation attributes) without a Neo4j round-trip.

    All values are ISO date strings compared lexicographically.
    None / empty string = open-ended (always valid on that side).

    Args:
        entity_from: Entity's effective_from (ISO string or empty)
        entity_to:   Entity's effective_to   (ISO string or empty)
        query_from:  Query window start      (ISO string or empty)
        query_to:    Query window end         (ISO string or empty)

    Returns:
        True if the entity's temporal range overlaps the query window.
    """
    ef = entity_from or ""
    et = entity_to or ""
    qf = query_from or ""
    qt = query_to or ""

    if not ef and not et:
        return True

    # Normalize for lexicographic comparison: truncate to shorter length
    # so "2025-01-01T00:00:00+00:00" vs "2025-01-01T00:00:00Z" compare correctly
    def _le(a: str, b: str) -> bool:
        """a <= b using prefix of shorter length."""
        n = min(len(a), len(b))
        return a[:n] <= b[:n]

    def _ge(a: str, b: str) -> bool:
        """a >= b using prefix of shorter length."""
        n = min(len(a), len(b))
        return a[:n] >= b[:n]

    if qf and qt:
        # Both query bounds
        if ef and et:
            return _le(ef, qt) and _ge(et, qf)
        elif ef:
            return _le(ef, qt)
        else:
            return _ge(et, qf)

    elif qf:
        # Only query start
        if et:
            return _ge(et, qf)  # entity must not have ended before query starts
        return True  # entity has no end (or no temporal) → still active

    elif qt:
        # Only query end
        if ef:
            return _le(ef, qt)  # entity must have started before query ends
        return True

    return True  # no query bounds → everything valid


class Neo4jService:
    """Service for Neo4j graph database operations."""

    _instance: Optional["Neo4jService"] = None
    _driver: Any = None

    def __new__(cls):
        """Singleton pattern."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        """Initialize Neo4j service."""
        if self._initialized:
            return

        settings = get_settings()
        self.embedding_service = get_embedding_service()

        # Initialize Neo4j connection
        try:
            self._driver = GraphDatabase.driver(
                settings.neo4j_uri,
                auth=(settings.neo4j_user, settings.neo4j_password),
            )
            # Test connection
            self._driver.verify_connectivity()
            logger.info(" Neo4j connected")
        except Exception as e:
            logger.error(f"Failed to connect to Neo4j: {e}")
            raise ConnectionError("Cannot connect to Neo4j") from e

        self._initialized = True
        logger.info("Neo4jService initialized")

    @classmethod
    def _get_cache_service(cls):
        """Get cache service (lazy import to avoid circular dependency)."""
        try:
            from .cache_service import get_cache_service

            return get_cache_service()
        except Exception as e:
            logger.debug(f"Could not get cache service: {e}")
            return None

    def _require_driver(self) -> Any:
        if self._driver is None:
            raise ConnectionError("Neo4j driver is not initialized")
        return self._driver

    def cypher_query(self, cypher: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute raw Cypher query (advanced use).

        Args:
            cypher: Cypher query string
            params: Query parameters

        Returns:
            Query results
        """
        try:
            driver = self._require_driver()
            with driver.session() as session:
                result = session.run(cast(Any, cypher), params or {}).data()
                return {
                    "status": "success",
                    "result_count": len(result),
                    "results": result,
                }
        except Exception as e:
            logger.error(f"Cypher query error: {e}")
            return {
                "status": "error",
                "message": str(e),
            }

    def attribute_search(self, node_id: str, node_type: str) -> dict[str, Any]:
        """Get attributes of a node and ALL its relationships.

        Returns node properties and all related nodes (both incoming and outgoing).
        No filtering - returns everything for reranking at higher level.

        Args:
            node_id: Node ID to query
            node_type: Node type/label

        Returns:
            Dict with:
            - node_props: Direct properties of the node (vectors excluded)
            - outgoing_relationships: List of all outgoing relationships with target nodes
            - incoming_relationships: List of all incoming relationships with source nodes
        """

        cypher_query = f"""
            MATCH (n:{node_type}:Entity {{id: $node_id}})

            OPTIONAL MATCH (n)-[r_out]->(related_out)
            OPTIONAL MATCH (n)<-[r_in]-(related_in)

            RETURN properties(n) AS node_props,
                   collect(DISTINCT {{
                       relationship_type: type(r_out),
                       direction: 'outgoing',
                       related_id: related_out.id,
                       related_name: related_out.name,
                       related_labels: labels(related_out),
                       related_properties: properties(related_out)
                   }}) AS outgoing_relationships,
                   collect(DISTINCT {{
                       relationship_type: type(r_in),
                       direction: 'incoming',
                       related_id: related_in.id,
                       related_name: related_in.name,
                       related_labels: labels(related_in),
                       related_properties: properties(related_in)
                   }}) AS incoming_relationships
            """

        neo4j_result = self.cypher_query(cypher_query, params={"node_id": node_id})

        # Filter vector fields from results
        if neo4j_result.get("status") == "success":
            results = neo4j_result.get("results", [])
            if results:
                for result in results:
                    # Keep node properties as-is (no longer filtering vector fields)

                    # Filter outgoing relationships - remove null entries and filter vectors
                    if "outgoing_relationships" in result and result["outgoing_relationships"]:
                        filtered_outgoing = []
                        for rel in result["outgoing_relationships"]:
                            # Skip null entries from collect()
                            if rel and rel.get("relationship_type"):
                                # Keep related_properties as-is
                                filtered_outgoing.append(rel)
                        result["outgoing_relationships"] = filtered_outgoing

                    # Filter incoming relationships - remove null entries and filter vectors
                    if "incoming_relationships" in result and result["incoming_relationships"]:
                        filtered_incoming = []
                        for rel in result["incoming_relationships"]:
                            # Skip null entries from collect()
                            if rel and rel.get("relationship_type"):
                                # Keep related_properties as-is
                                filtered_incoming.append(rel)
                        result["incoming_relationships"] = filtered_incoming

        logger.info(f"neo4j_result: {neo4j_result}")
        return neo4j_result

    def get_all_node_relationships(self, node_id: str, node_type: str) -> list[str]:
        """Get all relationship types for a node (both incoming and outgoing).

        This is used to collect all possible relationships before reranking.

        Args:
            node_id: Node ID to query
            node_type: Node type/label

        Returns:
            List of unique relationship type names

        Example:
             get_all_node_relationships("gdu_001", "University")
            ["HAS_ADDRESS", "HAS_CAMPUS", "HAS_FACULTY", "HAS_WEBSITE"]
        """
        try:
            cypher_query = f"""
                MATCH (n:{node_type}:Entity {{id: $node_id}})
                OPTIONAL MATCH (n)-[r_out]->()
                OPTIONAL MATCH (n)<-[r_in]-()
                WITH collect(DISTINCT type(r_out)) + collect(DISTINCT type(r_in)) AS all_rels
                RETURN [rel IN all_rels WHERE rel IS NOT NULL] AS relationships
                """

            result = self.cypher_query(cypher_query, params={"node_id": node_id})

            if result.get("status") == "success":
                results = result.get("results", [])
                if results and results[0]:
                    relationships = results[0].get("relationships", [])
                    logger.info(f"Node {node_id}: {len(relationships)} relationships - {relationships}")
                    return relationships

            logger.warning(f"Node {node_id}: No relationships found")
            return []

        except Exception as e:
            logger.error(f"Error getting node relationships: {e}")
            return []

    def enumeration_search_neo4j(self, node_id: str, node_type: str, relationship: str | None = None) -> dict[str, Any]:
        """Count and list nodes based on entity and optional relationship.

        Logic:
        - If relationship provided: Traverse via relationship to count + list related nodes
        - If no relationship: Count + list all nodes of the same type

        Args:
            node_id: Source node ID
            node_type: Source node type/label
            relationship: Optional relationship type to traverse

        Returns:
            Dict with count and items list containing node properties
        """
        try:
            logger.info(f"Enumeration search: node_type={node_type}, relationship={relationship}")

            if relationship:
                # Case 1: Count + list nodes via relationship
                cypher_query = f"""
                    MATCH (n:{node_type}:Entity {{id: $node_id}})-[r:{relationship}]->(m)
                    RETURN m.id AS node_id,
                           m.name AS name,
                           m.description AS description,
                           labels(m) AS labels,
                           properties(m) AS properties
                """
                params = {"node_id": node_id}
            else:
                # Case 2: Count + list all nodes of same type
                cypher_query = f"""
                    MATCH (m:{node_type}:Entity)
                    RETURN m.id AS node_id,
                           m.name AS name,
                           m.description AS description,
                           labels(m) AS labels,
                           properties(m) AS properties
                """
                params = {}
            logger.info(f"cypher_query: {cypher_query}, params: {params}")

            result = self.cypher_query(cypher_query, params=params)

            if result.get("status") != "success":
                logger.error(f"Enumeration query failed: {result.get('message')}")
                return {"status": "not_found", "count": 0, "items": []}

            results = result.get("results", [])

            # Format items
            formatted_items = []
            for item in results:
                formatted_items.append(
                    {
                        "node_id": item.get("node_id"),
                        "name": item.get("name"),
                        "description": item.get("description"),
                        "labels": item.get("labels", []),
                        "properties": item.get("properties", {}),
                    }
                )

            logger.info(f"Found {len(formatted_items)} items")

            return {"status": "success", "count": len(formatted_items), "items": formatted_items}

        except Exception as e:
            logger.error(f"Enumeration search error: {e}", exc_info=True)
            return {"status": "error", "count": 0, "items": [], "message": str(e)}

    def comparison_search_neo4j(
        self,
        node_ids: list[str],
        node_types: list[str],
        attribute_list: list[str] | None = None,
        effective_from: str | None = None,
        effective_to: str | None = None,
        time: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Compare multiple entities by retrieving their properties"""

        # Accept either explicit ISO strings OR a time dict (from YAML $input.time)
        if time and not (effective_from or effective_to):
            effective_from, effective_to = _normalize_temporal_params(time)

        # Prepare node data
        node_data = [
            {"node_id": node_id, "node_type": node_type}
            for node_id, node_type in zip(node_ids, node_types, strict=False)
        ]

        temporal_clause = build_temporal_where_clause("n", effective_from, effective_to)
        extra_where = f"AND {temporal_clause}" if temporal_clause else ""

        query = f"""
            UNWIND $node_data AS data
            MATCH (n {{id: data.node_id}})
            WHERE data.node_type IN labels(n){extra_where}
            RETURN n.id AS node_id,
                   labels(n) AS labels,
                   n.name AS name,
                   n.description AS description,
                   properties(n) AS properties
        """

        extra_params: dict[str, Any] = {}
        if effective_from:
            extra_params["effective_from"] = effective_from
        if effective_to:
            extra_params["effective_to"] = effective_to

        try:
            run_params: dict[str, Any] = {"node_data": node_data, **extra_params}
            driver = self._require_driver()
            with driver.session() as session:
                result = session.run(cast(Any, query), run_params).data()

                #  FIX: Convert protobuf to Python native types
                cleaned_results = []
                for record in result:
                    if attribute_list:
                        # Filter properties to only include requested attributes
                        filtered_properties = {k: v for k, v in record["properties"].items() if k in attribute_list}
                        record["properties"] = filtered_properties
                    else:
                        # Remove vector fields from properties
                        record["properties"] = dict(record["properties"])

                    cleaned = {
                        "node_id": record["node_id"],
                        "labels": list(record["labels"]),  # Convert protobuf list
                        "name": record["name"],
                        "description": record.get("description"),
                        "properties": dict(record["properties"]),  # Convert protobuf dict
                    }
                    cleaned_results.append(cleaned)

                return cleaned_results

        except Exception as e:
            traceback.print_exc()
            logger.error(f"Comparison search error: {e}")
            return []

    def get_graph_statistics(self) -> dict[str, Any]:
        """Get graph database statistics."""
        try:
            driver = self._require_driver()
            with driver.session() as session:
                stats = {}

                # Node count
                node_count = session.run("MATCH (n) RETURN count(n) as count").single()
                stats["total_nodes"] = node_count["count"] if node_count else 0

                # Relationship count
                rel_count = session.run("MATCH ()-[r]->() RETURN count(r) as count").single()
                stats["total_relationships"] = rel_count["count"] if rel_count else 0

                # Categories
                categories = session.run("""
                    MATCH (n)
                    WHERE (n:Entity OR n:Concept) AND n.category IS NOT NULL
                    RETURN DISTINCT n.category as category, count(*) as count
                    ORDER BY count DESC
                """).data()
                stats["categories"] = categories

                return {
                    "status": "success",
                    "statistics": stats,
                }

        except Exception as e:
            logger.error(f"Failed to get graph stats: {e}")
            return {
                "status": "error",
                "message": str(e),
            }

    def _cosine_similarity(self, vec1: list[float], vec2: list[float]) -> float:
        """Calculate cosine similarity."""
        import numpy as np

        vec1_np = np.array(vec1)
        vec2_np = np.array(vec2)

        dot_product = np.dot(vec1_np, vec2_np)
        norm1 = np.linalg.norm(vec1_np)
        norm2 = np.linalg.norm(vec2_np)

        if norm1 == 0 or norm2 == 0:
            return 0.0

        return float(dot_product / (norm1 * norm2))

    def close(self):
        """Close Neo4j connection."""
        if self._driver:
            self._driver.close()
            logger.info("Neo4j connection closed")

    def find_node_relationship(
        self,
        target_node_ids: list[str],
        candidate_node_ids: list[str] | None = None,
        candidate_labels: list[str] | None = None,
        max_depth: int = 5,
        include_intermediate: bool = True,
        effective_from: str | None = None,
        effective_to: str | None = None,
        time: dict[str, Any] | None = None,
        apply_temporal_to_intermediate: bool = False,
    ) -> dict[str, Any]:
        """
        Find relationships between target and candidate nodes.
        Supports CASCADING SEARCH for multi-hop label discovery.

        Temporal args (any format accepted, auto-converted to ISO):
            effective_from: Year string "2025", int 2025, or ISO "2025-01-01T00:00:00Z"
            effective_to: Same formats as effective_from
            time: Dict from YAML e.g. {"mode": "year", "year": 2025}
                  or {"mode": "range", "from_year": 2024, "to_year": 2026}
            apply_temporal_to_intermediate: Filter intermediate nodes by temporal constraints
        """
        effective_from, effective_to = _normalize_temporal_params(time=time)

        logger.info(" Finding relationships:")
        logger.info(f"Targets: {len(target_node_ids)} nodes")
        logger.info(f"Candidates: {candidate_node_ids if candidate_node_ids else candidate_labels}")

        #  Log temporal filtering status
        if effective_from or effective_to:
            logger.info(f" Temporal Filter: {effective_from}  {effective_to}")
            logger.info(f" Filter intermediates: {apply_temporal_to_intermediate}")

        # ========================================
        # MODE 1: IDs mode (specific candidates)
        # ========================================
        if candidate_node_ids:
            # Direct search with specific IDs
            direct_connections = self._check_direct_connection(
                target_node_ids=target_node_ids,
                candidate_node_ids=candidate_node_ids,
                effective_from=effective_from,  #  Pass temporal params
                effective_to=effective_to,  #  Pass temporal params
            )

            # Extract direct IDs to exclude from indirect
            direct_candidate_ids = set()
            if isinstance(direct_connections, dict):
                for target_group in direct_connections.values():
                    for candidate in target_group.get("candidates", []):
                        direct_candidate_ids.add(candidate.get("candidate_id"))

            # Find indirect for remaining IDs
            remaining_ids = [cid for cid in candidate_node_ids if cid not in direct_candidate_ids]

            if remaining_ids and max_depth > 1:
                indirect_connections = self._find_shortest_path(
                    target_node_ids=target_node_ids,
                    candidate_node_ids=remaining_ids,
                    max_depth=max_depth,
                    include_intermediate=include_intermediate,
                    effective_from=effective_from,  #  Pass temporal params
                    effective_to=effective_to,  #  Pass temporal params
                    apply_temporal_to_intermediate=apply_temporal_to_intermediate,  #  Pass temporal params
                )
            else:
                indirect_connections = {}

            return {
                "direct_connections": sanitize_neo4j_types(direct_connections),
                "indirect_connections": sanitize_neo4j_types(indirect_connections),
            }

        # ========================================
        # MODE 2: Labels mode (CASCADING SEARCH)
        # ========================================
        elif candidate_labels:
            # Step 1: Find direct connections for ALL labels
            logger.info(f"Step 1: Finding direct for labels: {candidate_labels}")

            initial_direct = self._check_direct_connection(
                target_node_ids=target_node_ids,
                candidate_labels=candidate_labels,
                effective_from=effective_from,  #  Pass temporal params
                effective_to=effective_to,  #  Pass temporal params
            )

            # Check if we found ANY direct connections
            if not initial_direct or (isinstance(initial_direct, dict) and len(initial_direct) == 0):
                logger.info(" No direct connections, searching with _find_shortest_path")

                indirect_connections = self._find_shortest_path(
                    target_node_ids=target_node_ids,
                    candidate_labels=candidate_labels,
                    max_depth=max_depth,
                    include_intermediate=include_intermediate,
                    effective_from=effective_from,  #  Pass temporal params
                    effective_to=effective_to,  #  Pass temporal params
                    apply_temporal_to_intermediate=apply_temporal_to_intermediate,  #  Pass temporal params
                )

                return {"direct_connections": {}, "indirect_connections": indirect_connections}

            labels_with_direct = set()
            labels_without_direct = set(candidate_labels)
            direct_node_ids = []

            if isinstance(initial_direct, dict):
                for target_group in initial_direct.values():
                    for candidate in target_group.get("candidates", []):
                        node_type = candidate.get("candidate_properties", {}).get("node_type")
                        node_id = candidate.get("candidate_id")

                        if node_type:
                            labels_with_direct.add(node_type)
                            labels_without_direct.discard(node_type)

                        if node_id:
                            direct_node_ids.append(node_id)

            logger.info(f"Labels with direct: {labels_with_direct}")
            logger.info(f"Labels without direct: {labels_without_direct}")
            logger.info(f"Found {len(direct_node_ids)} direct nodes")

            # Step 2: Cascading search for remaining labels
            indirect_connections = {}

            if labels_without_direct and direct_node_ids:
                logger.info(
                    f" Step 2: Cascading search for {labels_without_direct} via {len(direct_node_ids)} direct nodes"
                )

                cascading_results = self._check_direct_connection(
                    target_node_ids=direct_node_ids,
                    candidate_labels=list(labels_without_direct),
                    effective_from=effective_from,  #  Pass temporal params to cascading
                    effective_to=effective_to,  #  Pass temporal params to cascading
                )

                if cascading_results and isinstance(cascading_results, dict):
                    # Convert cascading results to indirect format

                    for cascade_target_id, cascade_group in cascading_results.items():
                        candidates = cascade_group.get("candidates", [])

                        if not candidates:
                            continue

                        # Find which original target this cascade_target belongs to
                        original_target_id = None
                        intermediate_node_info = None

                        for orig_target_id, orig_group in initial_direct.items():
                            for direct_candidate in orig_group.get("candidates", []):
                                if direct_candidate.get("candidate_id") == cascade_target_id:
                                    original_target_id = orig_target_id
                                    intermediate_node_info = {
                                        "node_id": cascade_target_id,
                                        "node_name": direct_candidate.get("candidate_name", ""),
                                        "node_type": direct_candidate.get("candidate_properties", {}).get("node_type"),
                                    }
                                    break
                            if original_target_id:
                                break

                        if not original_target_id:
                            logger.error(f" Cannot trace {cascade_target_id} back to original target")
                            continue

                        # Initialize indirect group
                        if original_target_id not in indirect_connections:
                            indirect_connections[original_target_id] = {
                                "target_id": original_target_id,
                                "target_name": initial_direct[original_target_id].get("target_name", ""),
                                "target_properties": initial_direct[original_target_id].get("target_properties", {}),
                                "candidates": [],
                            }

                        # Add cascading candidates as indirect (hop=2)
                        for candidate in candidates:
                            indirect_candidate = candidate.copy()
                            indirect_candidate["graph_hop"] = 2
                            indirect_candidate["intermediate_nodes"] = (
                                [intermediate_node_info] if intermediate_node_info else []
                            )

                            indirect_connections[original_target_id]["candidates"].append(indirect_candidate)

                    logger.info(
                        f"  Cascading found {sum(len(g.get('candidates', [])) for g in indirect_connections.values())} indirect connections"
                    )
                else:
                    logger.info(" No cascading connections found for remaining labels")

            # Summary logging
            direct_count_by_type = {}
            indirect_count_by_type = {}

            if isinstance(initial_direct, dict):
                for tg in initial_direct.values():
                    for c in tg.get("candidates", []):
                        nt = c.get("candidate_properties", {}).get("node_type")
                        direct_count_by_type[nt] = direct_count_by_type.get(nt, 0) + 1

            if isinstance(indirect_connections, dict):
                for tg in indirect_connections.values():
                    for c in tg.get("candidates", []):
                        nt = c.get("candidate_properties", {}).get("node_type")
                        indirect_count_by_type[nt] = indirect_count_by_type.get(nt, 0) + 1

            logger.info(f"FINAL: Direct={direct_count_by_type}, Indirect={indirect_count_by_type}")
            logger.info(f"initial_direct: {initial_direct}")
            return {
                "direct_connections": sanitize_neo4j_types(initial_direct),
                "indirect_connections": sanitize_neo4j_types(indirect_connections),
            }

        else:
            raise ValueError("Must provide either candidate_node_ids or candidate_labels")

    def _check_direct_connection(
        self,
        target_node_ids: list[str],
        candidate_node_ids: list[str] | None = None,
        candidate_labels: list[str] | None = None,
        effective_from: str | None = None,  #  NEW
        effective_to: str | None = None,  #  NEW
    ) -> dict[str, Any]:
        """
        Check for direct connections.
        Groups results BY TARGET to avoid duplicate target_properties.

        Supports both:
        - MODE 1: candidate_node_ids (specific IDs)
        - MODE 2: candidate_labels (all nodes of certain types)

        New Args:
            effective_from: Start of validity period (ISO UTC format)
            effective_to: End of validity period (ISO UTC format)

        Returns:
            {
                "target_id_1": {
                    "target_id": "...",
                    "target_name": "...",
                    "target_properties": {...},
                    "candidates": [...]
                }
            }
        """

        #  Build temporal WHERE clauses
        # 1) Relationship temporal: filter r.effective_from/to (e.g. APPLIES_TO on AdmissionMethod)
        # 2) Candidate node temporal: filter candidate.effective_from/to (e.g. Policy node itself)
        # Combined with AND: row passes only if BOTH rel and node are temporally valid.
        relationship_temporal_filter = build_temporal_where_clause("r", effective_from, effective_to)
        candidate_temporal_filter = build_temporal_where_clause("candidate", effective_from, effective_to)
        target_temporal_filter = build_temporal_where_clause("target", effective_from, effective_to)
        params: dict[str, Any] = {}

        if candidate_node_ids:
            #  MODE 1: IDs mode (FASTEST - specific candidates)

            #  Build WHERE clause for relationship + candidate + target node temporal filter
            where_parts = []
            if relationship_temporal_filter:
                where_parts.append(relationship_temporal_filter)
            if candidate_temporal_filter:
                where_parts.append(candidate_temporal_filter)
            if target_temporal_filter:
                where_parts.append(target_temporal_filter)
            where_clause = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

            query = f"""
            UNWIND $candidate_ids AS candidate_id
            UNWIND $target_ids AS target_id
            MATCH (candidate {{id: candidate_id}})-[r]-(target {{id: target_id}})
            {where_clause}

            RETURN
                candidate.id AS candidate_id,
                candidate.name AS candidate_name,
                target_id,
                target.name AS target_name,
                labels(candidate)[0] AS candidate_label,
                labels(target)[0] AS target_label,
                properties(candidate) AS candidate_properties,
                type(r) AS relationship_type,
                properties(r) AS relationship_properties,
                elementId(r) AS relationship_id,
                properties(target) AS target_properties
            """

            params = {"candidate_ids": candidate_node_ids, "target_ids": target_node_ids}

        elif candidate_labels:
            #  MODE 2: Labels mode (flexible - by type)

            #  Build combined WHERE clause (label filter + relationship temporal + candidate node temporal)
            where_parts = ["labels(candidate)[0] IN $candidate_labels"]
            if relationship_temporal_filter:
                where_parts.append(relationship_temporal_filter)
            if candidate_temporal_filter:
                where_parts.append(candidate_temporal_filter)
            if target_temporal_filter:
                where_parts.append(target_temporal_filter)

            combined_where = "WHERE " + " AND ".join(where_parts)

            query = f"""
            UNWIND $target_ids AS target_id
            MATCH (target {{id: target_id}})
            MATCH (candidate)-[r]-(target)
            {combined_where}

            RETURN
                candidate.id AS candidate_id,
                candidate.name AS candidate_name,
                target_id,
                target.name AS target_name,
                labels(candidate)[0] AS candidate_label,
                labels(target)[0] AS target_label,
                properties(candidate) AS candidate_properties,
                type(r) AS relationship_type,
                properties(r) AS relationship_properties,
                elementId(r) AS relationship_id,
                properties(target) AS target_properties
            """

            params: dict[str, Any] = {"target_ids": target_node_ids, "candidate_labels": candidate_labels}

        else:
            raise ValueError("Must provide either candidate_node_ids or candidate_labels")

        #  Add temporal parameters if present
        if effective_from:
            params["effective_from"] = effective_from
        if effective_to:
            params["effective_to"] = effective_to

        try:
            # logger.error(f"query:{query}")
            # logger.error(f"params:{params}")
            driver = self._require_driver()
            with driver.session() as session:
                result = session.run(cast(Any, query), params)

                #  GROUP BY TARGET_ID to avoid duplication
                grouped_results = {}
                # logger.error(f"Direct connection raw results: {result}")

                for record in result:
                    # logger.error(f"Direct connection raw results: {record}")
                    target_id = record["target_id"]

                    # Initialize target group if not exists
                    if target_id not in grouped_results:
                        grouped_results[target_id] = {
                            "target_id": target_id,
                            "target_name": record.get("target_name", ""),
                            "target_description": record["target_properties"].get("description", ""),
                            "target_properties": dict(record["target_properties"]),
                            "candidates": [],
                        }
                        # Add node_type to target_properties
                        grouped_results[target_id]["target_properties"]["node_type"] = record.get("target_label", "")

                    # Build candidate info
                    candidate_info = {
                        "candidate_id": record["candidate_id"],
                        "candidate_name": record.get("candidate_name", ""),
                        "candidate_properties": dict(record["candidate_properties"]),
                        "relationship_type": record["relationship_type"],
                        "relationship_properties": dict(record["relationship_properties"])
                        if record["relationship_properties"]
                        else {},
                        "graph_hop": 1,  # Direct = always hop 1,
                        "relationship_id": record["relationship_id"],
                    }

                    # Add node_type to candidate_properties
                    candidate_info["candidate_properties"]["node_type"] = record["candidate_label"]

                    # Add candidate to this target's list
                    grouped_results[target_id]["candidates"].append(candidate_info)

                # Log summary
                total_candidates = sum(len(g["candidates"]) for g in grouped_results.values())
                temporal_status = " (temporal filtered)" if (effective_from or effective_to) else ""
                logger.info(
                    f"Found {total_candidates} direct connections grouped into {len(grouped_results)} targets{temporal_status}"
                )
                logger.info(f"grouped_results: {grouped_results}")
                return grouped_results

        except Exception as e:
            logger.error(f"Error checking direct connections: {e}")
            traceback.print_exc()
            return {}

    def _find_shortest_path(
        self,
        target_node_ids: list[str],
        candidate_node_ids: list[str] | None = None,
        candidate_labels: list[str] | None = None,
        max_depth: int = 5,
        include_intermediate: bool = True,
        effective_from: str | None = None,  #  NEW
        effective_to: str | None = None,  #  NEW
        apply_temporal_to_intermediate: bool = True,  #  NEW
        need_to_use_min_hop: bool = True,  # 🆕 NEW: Force use of minHop for testing
		allowed_rels: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Find shortest paths between targets and candidates.
        Groups results BY TARGET to avoid duplicate target_properties.

        Supports both:
        - MODE 1: candidate_node_ids (specific IDs via Milvus search)
        - MODE 2: candidate_labels (all nodes of certain types)

        New Args:
            effective_from: Start of validity period (ISO UTC format)
            effective_to: End of validity period (ISO UTC format)
            apply_temporal_to_intermediate: Filter intermediate nodes by temporal constraints

        Returns:
            {
                "target_id_1": {
                    "target_id": "...",
                    "target_name": "...",
                    "target_properties": {...},
                    "candidates": [...]
                }
            }
        """

        #  Build temporal WHERE clause for RELATIONSHIPS in path
        # Temporal filter applies to relationships, NOT nodes.
        # Nodes are always included regardless of their temporal properties.
        # Only relationships with temporal properties outside the query window are excluded.
        rel_temporal_filter = ""
        if effective_from or effective_to:
            rel_temporal = build_temporal_where_clause("rel", effective_from, effective_to)
            if rel_temporal:
                rel_temporal_filter = f"\nAND ALL(rel IN relationships(path) WHERE {rel_temporal})"
                logger.info(f"rel_temporal_filter: {rel_temporal_filter}")

        #  Build temporal filter for intermediate nodes (optional)
        intermediate_temporal_filter = ""
        logger.info(
            f"apply_temporal_to_intermediate : {apply_temporal_to_intermediate}, effective_from: {effective_from}, effective_to: {effective_to}"
        )
        node_temporal = build_temporal_where_clause("node", effective_from, effective_to)
        candidate_temporal_filter = build_temporal_where_clause("candidate", effective_from, effective_to)
        logger.info(f"node_temporal_filter: {node_temporal}")
        if apply_temporal_to_intermediate and (effective_from or effective_to):
            if node_temporal:
                intermediate_temporal_filter = f"\nAND ALL(node IN nodes(path) WHERE {node_temporal})"
                logger.info(f"intermediate_temporal_filter: {intermediate_temporal_filter}")

        params: dict[str, Any] = {}
        # 🆕 Build relationship type pattern for inline Cypher filtering
        rel_type_pattern = ""
        if allowed_rels:
            rel_type_pattern = ":" + "|".join(allowed_rels)

        if candidate_node_ids:
            #  MODE 1: IDs mode (FASTEST - use original shortestPath algorithm)

            #  Temporal is now applied to relationships in path, not to endpoint nodes

            # Build combined path filter (relationships + optionally intermediate nodes)
            path_filter_parts = []
            if effective_from or effective_to:
                path_filter_parts.append(candidate_temporal_filter.strip())
            if rel_temporal_filter:
                path_filter_parts.append(rel_temporal_filter.strip())
            if intermediate_temporal_filter:
                path_filter_parts.append(intermediate_temporal_filter.strip())
            path_where = ""
            if path_filter_parts:
                path_where = "WHERE true AND " + " ".join(path_filter_parts)

            query = f"""
            UNWIND $candidate_ids AS candidate_id
            UNWIND $target_ids AS target_id
            MATCH path = allShortestPaths(
                (candidate {{id: candidate_id}})-[{rel_type_pattern}*1..{max_depth}]-(target {{id: target_id}})
            )
            {path_where}

            WITH 
                candidate,
                target,
                candidate_id,
                target_id,
                path,
                candidate.name as candidate_name,
                properties(candidate) AS candidate_properties,
                target.name as target_name,
                properties(target) AS target_properties,
                length(path) as path_length
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
                [node in nodes(path)[1..-1] | {{
                    node_id: node.id,
                    node_name: node.name,
                    node_type: labels(node)[0]
                }}] as intermediate_nodes,
                [rel in relationships(path) | type(rel)] as relationship_types,
                [rel in relationships(path) | {{
                    type: type(rel),
                    properties: properties(rel),
                    source_id: startNode(rel).id,
                    target_id: endNode(rel).id,
                    source_name: startNode(rel).name,
                    target_name: endNode(rel).name
                }}] as relationship_details
            """

            params = {"candidate_ids": candidate_node_ids, "target_ids": target_node_ids}

        elif candidate_labels:
            if "Person" in candidate_labels:
                max_depth = 2
            # (0.4s vs 12.7s old approach: avoids UNWIND over all candidate IDs)
            #
            # Uses Cartesian product (target, candidate:Label) then allShortestPaths.
            # allShortestPaths (not shortestPath!) is required because a candidate
            # (EducationSystem) and some not. shortestPath picks only one path
            # non-deterministically and could pick the blocked one, causing the
            # candidate to be incorrectly filtered out in post-processing.

            # Build combined path filter for MODE 2 (relationships + optionally intermediate nodes)
            # Note: temporal filtering applies to RELATIONSHIPS in path, not to endpoint nodes.
            path_filter_parts_m2 = []
            if effective_from or effective_to:
                path_filter_parts_m2.append(candidate_temporal_filter.strip())
            if rel_temporal_filter:
                path_filter_parts_m2.append(rel_temporal_filter.strip())
            if intermediate_temporal_filter:
                path_filter_parts_m2.append(intermediate_temporal_filter.strip())
            path_where_m2 = ""
            if path_filter_parts_m2:
                path_where_m2 = "WHERE true AND " + " ".join(path_filter_parts_m2)

            query = f"""
            UNWIND $target_ids AS target_id
            MATCH (target {{id: target_id}})

            // Bind candidates by label, then find all shortest paths
            MATCH (candidate)
            WHERE labels(candidate)[0] IN $candidate_labels

            MATCH path = allShortestPaths((target)-[{rel_type_pattern}*1..{max_depth}]-(candidate))
            {path_where_m2}

            WITH
                candidate,
                target,
                candidate.id as candidate_id,
                target_id,
                path,
                candidate.name as candidate_name,
                properties(candidate) AS candidate_properties,
                target.name as target_name,
                properties(target) AS target_properties,
                length(path) as path_length
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
                // Reverse: path runs target→candidate but downstream expects candidate→target order
                reverse([node in nodes(path)[1..-1] | {{
                    node_id: node.id,
                    node_name: node.name,
                    node_type: labels(node)[0]
                }}]) as intermediate_nodes,
                reverse([rel in relationships(path) | type(rel)]) as relationship_types,
                reverse([rel in relationships(path) | {{
                    type: type(rel),
                    properties: properties(rel),
                    source_id: startNode(rel).id,
                    target_id: endNode(rel).id,
                    source_name: startNode(rel).name,
                    target_name: endNode(rel).name
                }}]) as relationship_details
            """

            params: dict[str, Any] = {"target_ids": target_node_ids, "candidate_labels": candidate_labels}

        else:
            raise ValueError("Must provide either candidate_node_ids or candidate_labels")

        #  Add temporal parameters if present
        if effective_from:
            params["effective_from"] = effective_from
        if effective_to:
            params["effective_to"] = effective_to

        try:
            driver = self._require_driver()
            with driver.session() as session:
                # logger.error(f"query:{query}")
                # logger.error(f"params indirect:{params}")
                # logger.error(f"type of effective_from: {type(params["effective_from"])})")
                result = session.run(cast(Any, query), params)

                #  GROUP BY TARGET_ID to avoid duplication
                grouped_results = {}

                for record in result:
                    target_id = record["target_id"]

                    # Initialize target group if not exists
                    if target_id not in grouped_results:
                        grouped_results[target_id] = {
                            "target_id": target_id,
                            "target_name": record.get("target_name", ""),
                            "target_label": record.get("target_label", ""),
                            "target_description": record["target_properties"].get("description", ""),
                            "target_properties": dict(record["target_properties"]),
                            "candidates": [],
                        }
                        grouped_results[target_id]["target_properties"]["node_type"] = record.get("target_label", "")

                    # Build candidate info
                    # Clean relationship details (remove embeddings)
                    rel_details = []
                    for rd in record.get("relationship_details", []):
                        rd_dict = dict(rd)
                        props = dict(rd_dict.get("properties", {}))
                        props.pop("embedding", None)
                        rd_dict["properties"] = props
                        rel_details.append(rd_dict)

                    candidate_info = {
                        "candidate_id": record["candidate_id"],
                        "candidate_name": record.get("candidate_name", ""),
                        "candidate_properties": dict(record["candidate_properties"]),
                        "graph_hop": record["path_length"],
                        "relationship_types": record["relationship_types"],
                        "relationship_details": rel_details,
                    }

                    # Add node_type to candidate_properties
                    candidate_info["candidate_properties"]["node_type"] = record["candidate_label"]

                    # Add intermediate nodes if requested
                    if include_intermediate:
                        candidate_info["intermediate_nodes"] = record["intermediate_nodes"]

                    # Add candidate to this target's list
                    grouped_results[target_id]["candidates"].append(candidate_info)

                # Log summary before filtering
                total_candidates = sum(len(g["candidates"]) for g in grouped_results.values())
                temporal_status = " (temporal filtered)" if (effective_from or effective_to) else ""
                logger.info(
                    f"Found {total_candidates} paths grouped into {len(grouped_results)} targets{temporal_status}"
                )

                #  Apply min_hop filtering
                grouped_results = (
                    self._filter_candidates_by_min_hop(grouped_results) if need_to_use_min_hop else grouped_results
                )

                return grouped_results

        except Exception as e:
            logger.error(f"Error finding shortest paths: {e}")
            traceback.print_exc()
            return {}

    def _filter_candidates_by_min_hop(self, grouped_results: dict[str, Any]) -> dict[str, Any]:
        for target_id, target_group in grouped_results.items():
            candidates = target_group.get("candidates", [])
            if not candidates:
                continue

            # Step 1: Find min_hop across all candidates for this target
            min_hop = min(c["graph_hop"] for c in candidates)

            # Step 2: Get target label
            target_label = target_group.get("target_label", "")

            # Step 3: Filter candidates
            filtered = []
            for candidate in candidates:
                candidate_label = candidate.get("candidate_properties", {}).get("node_type", "")
                path_length = candidate["graph_hop"]

                # From University, Person nodes connect at different hops:
                # All are valid; min_hop=1 would incorrectly drop hop-2 persons.
                is_person_from_university = (candidate_label == "Person" and target_label == "University") or (
                    target_label == "Person" and candidate_label == "University"
                )
                if is_person_from_university:
                    filtered.append(candidate)
                    continue

                # Check if target OR candidate is Specialization
                is_specialization = candidate_label == "Specialization" or target_label == "Specialization"

                if is_specialization:
                    # Rule 2: allow up to min_hop
                    # min_hop += 1
                    if path_length <= min_hop + 1:
                        filtered.append(candidate)
                else:
                    # Rule 1: strict min_hop only
                    if path_length == min_hop:
                        filtered.append(candidate)

            logger.info(
                f"  min_hop filter - Target {target_id} (label={target_label}): "
                f"{len(candidates)} → {len(filtered)} candidates (min_hop={min_hop})"
            )
            target_group["candidates"] = filtered

        # Remove target groups with no candidates left
        return {tid: tg for tid, tg in grouped_results.items() if tg.get("candidates")}

    def get_node_attribute(
        self,
        node_ids: list[str],
        attributes_keys: list[str] | None = None,
        effective_from: str | None = None,
        effective_to: str | None = None,
        time: dict[str, Any] | None = None,
    ) -> dict[str, dict[str, Any] | None]:

        # Accept either explicit ISO strings OR a time dict (from YAML $input.time)
        if time and not (effective_from or effective_to):
            effective_from, effective_to = _normalize_temporal_params(time)

        try:
            if not node_ids:
                return {}

            temporal_clause = build_temporal_where_clause("n", effective_from, effective_to)
            where_clause = f"WHERE {temporal_clause}" if temporal_clause else ""

            cypher = f"""
                UNWIND $node_ids AS node_id
                MATCH (n {{id: node_id}})
                {where_clause}
                RETURN 
                    node_id,
                    properties(n) AS attributes,
                    labels(n) AS labels
            """

            params: dict[str, Any] = {"node_ids": node_ids}
            if effective_from:
                params["effective_from"] = effective_from
            if effective_to:
                params["effective_to"] = effective_to

            result = self.cypher_query(cypher, params=params)

            node_attributes = {}

            if result.get("status") == "success" and result.get("results"):
                for r in result["results"]:
                    node_id = r.get("node_id")
                    props = r.get("attributes", {})
                    labels = r.get("labels", [])

                    if attributes_keys:
                        filtered_attrs = {}
                        name = props.get("name")

                        for k in attributes_keys:
                            if k == "labels":
                                filtered_attrs["labels"] = labels
                            elif k in props:
                                filtered_attrs[k] = props[k]
                        filtered_attrs["name"] = name

                        node_attributes[node_id] = filtered_attrs
                        logger.info(f"Node ID: {node_id}, Name: {name}, Filtered Attrs: {node_attributes}")

                    else:
                        # attributes_keys is None or empty list
                        filtered_attrs = {}

                        if "name" in props:
                            filtered_attrs["name"] = props["name"]
                        if "description" in props:
                            filtered_attrs["description"] = props["description"]

                        node_attributes[node_id] = filtered_attrs

            for node_id in node_ids:
                if node_id not in node_attributes:
                    node_attributes[node_id] = None

            return node_attributes

        except Exception as e:
            logger.error(f"Error getting node attributes (batch): {e}", exc_info=True)
            return {node_id: None for node_id in node_ids}

    def get_node_attributes_for_query_plan(
        self,
        node_ids: list[str],
        attribute_keys: list[str] | None = None,
        effective_from: str | None = None,
        effective_to: str | None = None,
        time: dict[str, Any] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """
        Fetch only the requested direct node attributes for query-plan attribute lookup.

        Returns a compact mapping:
            {node_id: {attribute_key: attribute_value}}

        Nodes without any requested direct attributes are omitted so the caller can
        distinguish "node found but requested attrs missing" from "attrs found".
        """
        if time and not (effective_from or effective_to):
            effective_from, effective_to = _normalize_temporal_params(time)

        try:
            clean_node_ids = [node_id for node_id in dict.fromkeys(node_ids or []) if node_id]
            clean_attribute_keys = [key for key in dict.fromkeys(attribute_keys or []) if key]
            if "name" not in clean_attribute_keys:
                clean_attribute_keys.append("name")

            if not clean_node_ids or not clean_attribute_keys:
                return {}

            logger.info(
                "get_node_attributes_for_query_plan: node_ids=%s, requested_attribute_keys=%s, "
                "effective_from=%s, effective_to=%s",
                clean_node_ids,
                clean_attribute_keys,
                effective_from,
                effective_to,
            )

            raw_attributes = self.get_node_attribute(
                node_ids=clean_node_ids,
                attributes_keys=clean_attribute_keys,
                effective_from=effective_from,
                effective_to=effective_to,
            )

            matched_attributes: dict[str, dict[str, Any]] = {}
            for node_id, attrs in raw_attributes.items():
                if not isinstance(attrs, dict):
                    continue

                filtered_attrs = {key: attrs[key] for key in clean_attribute_keys if key in attrs}
                if filtered_attrs:
                    matched_attributes[node_id] = filtered_attrs

            logger.info(
                "get_node_attributes_for_query_plan: matched_attributes=%s",
                matched_attributes,
            )

            return matched_attributes

        except Exception as e:
            logger.error(f"Error getting query-plan node attributes: {e}", exc_info=True)
            return {}

    def resolve_attribute_missing_major(
        self, entity_names: list[str], entity_type: str, attributes: list[str], time: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """
        Resolve missing attributes for a given entity by its name and type.
        This is a fallback method when the major attribute is missing.

        Args:
            entity_names: The name of the entity to resolve.
            entity_type: The type/label of the entity (e.g., "Course", "Topic").
            attributes: List of attributes to retrieve (e.g., ["description", "name"]).
            time: Optional time dict {"mode": "year", "year": 2025} for temporal filtering.

        Returns:
            A dictionary of resolved attributes or None if not found.
        """
        try:
            # logger.error(f"resolve_attribute_missing_major time: {time}")
            # logger.error(f"resolve_attribute_missing_major entity_name: {entity_names}")
            # logger.error(f"resolve_attribute_missing_major entity_type: {entity_type}")
            target_node = search_entity_by_name(
                entity_name=entity_names, entity_type=entity_type, threshold=0.5, top_k=5
            )
            if target_node:
                logger.info(f"Found target node for {entity_type} '{entity_names}': {target_node}")
                target_node_ids = [str(node_id) for node in target_node if (node_id := node.get("node_id"))]
                logger.info(f"target_node_ids: {target_node_ids}")

                effective_from, effective_to = _normalize_temporal_params(time)
                # logger.error(f"Effective_from: {effective_from}, Effective_to: {effective_to}")

                additional_attributes = ["academic_cohort", "code", "training_mode"]

                attributes.extend(additional_attributes)

                resolved_attrs = self.get_node_attribute(
                    node_ids=target_node_ids,
                    attributes_keys=attributes,
                    effective_from=effective_from,
                    effective_to=effective_to,
                )
                logger.info(f"Resolved attributes for {entity_type} '{entity_names}': {resolved_attrs}")
                return resolved_attrs

            return None

        except Exception as e:
            logger.error(f"Error resolving missing attribute for {entity_type} '{entity_names}': {e}", exc_info=True)
            return None


def get_neo4j_service() -> Neo4jService:
    """Get singleton Neo4jService instance."""
    return Neo4jService()
