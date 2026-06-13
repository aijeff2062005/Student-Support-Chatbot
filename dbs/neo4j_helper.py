"""
Neo4j Helper for EDU Agent - Hybrid Vector + Graph Search.

Supports:
- Vector similarity search on nodes
- Graph traversal for relationships
- Hybrid semantic + structural queries
"""

from typing import Any

from neo4j import GraphDatabase

from configs.config_service import get_settings
from utils.logging_config import get_logger

settings = get_settings()

logger = get_logger(__name__)

# Neo4j Configuration
NEO4J_URI = settings.neo4j_uri
NEO4J_USER = settings.neo4j_user
NEO4J_PASSWORD = settings.neo4j_password

# Global driver
neo4j_driver = None


def connect_neo4j():
    """Connect to Neo4j database."""
    global neo4j_driver
    if neo4j_driver is None:
        neo4j_driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
        logger.info(f"Connected to Neo4j at {NEO4J_URI}")
    return neo4j_driver


def disconnect_neo4j():
    """Disconnect from Neo4j."""
    global neo4j_driver
    if neo4j_driver:
        neo4j_driver.close()
        neo4j_driver = None
        logger.info(" Disconnected from Neo4j")


def cosine_similarity(vec1: list[float], vec2: list[float]) -> float:
    """Calculate cosine similarity between two vectors."""
    import numpy as np

    v1 = np.array(vec1)
    v2 = np.array(vec2)

    dot_product = np.dot(v1, v2)
    norm_v1 = np.linalg.norm(v1)
    norm_v2 = np.linalg.norm(v2)

    if norm_v1 == 0 or norm_v2 == 0:
        return 0.0

    return float(dot_product / (norm_v1 * norm_v2))


def get_graph_statistics() -> dict[str, Any]:
    """Get statistics about the graph database."""
    driver = connect_neo4j()

    with driver.session() as session:
        # Count nodes (Entity or Concept)
        node_count = session.run("MATCH (n) WHERE n:Entity OR n:Concept RETURN count(n) AS count").single()["count"]

        # Count nodes with embeddings
        embedded_count = session.run("""
            MATCH (n)
            WHERE (n:Entity OR n:Concept) AND n.embedding IS NOT NULL
            RETURN count(n) AS count
        """).single()["count"]

        # Count relationships
        rel_count = session.run("MATCH ()-[r]->() RETURN count(r) AS count").single()["count"]

        # Get types (entity types or categories)
        types = session.run("""
            MATCH (n)
            WHERE n:Entity OR n:Concept
            RETURN DISTINCT COALESCE(n.type, n.category, 'Unknown') AS type, count(*) AS count
            ORDER BY count DESC
        """)

        category_stats = [{"category": r["type"], "count": r["count"]} for r in types]

        return {
            "total_concepts": node_count,
            "concepts_with_embeddings": embedded_count,
            "total_relationships": rel_count,
            "categories": category_stats,
        }


def mixed_search_list(source_node_id: str, target_topic: str, limit: int = 30) -> list[dict[str, Any]]:
    """
    Retrieve a list of related entities from Neo4j based on target topic.
    Returns full properties for LLM context.
    source_node_id is a UUID string (property 'id' on Neo4j nodes).
    """
    if source_node_id is None:
        return []

    driver = connect_neo4j()
    target_label = target_topic

    query = f"""
    MATCH (a {{id: $source_id}})-[r]-(b:{target_label})
    RETURN b.name as name,
           b.id as id,
           b.description as description,
           labels(b) as labels,
           type(r) as relationship_type,
           properties(r) as relationship_properties,
           properties(b) as properties
    LIMIT $limit
    """

    try:
        with driver.session() as session:
            result = session.run(query, source_id=source_node_id, limit=limit)
            results = []
            for r in result:
                item = {
                    "name": r["name"],
                    "id": r["id"],
                    "description": r["description"],
                    "labels": list(r["labels"]),
                    "relationship_type": r["relationship_type"],
                    "relationship_properties": dict(r["relationship_properties"])
                    if r["relationship_properties"]
                    else {},
                    "properties": dict(r["properties"]) if r["properties"] else {},
                }
                results.append(item)
            logger.info(
                f"mixed_search_list found {len(results)} items for label '{target_label}' (source: {source_node_id})"
            )
            return results
    except Exception as e:
        logger.error(f"Error in mixed_search_list: {e}")
        return []


def mixed_search_count(source_node_id: str, target_topic: str, example_limit: int = 30) -> dict[str, Any]:
    """
    Count related entities and return examples.
    source_node_id is a UUID string (property 'id' on Neo4j nodes).
    """
    if source_node_id is None:
        return {"total": 0, "examples": []}

    driver = connect_neo4j()
    target_label = target_topic

    # Use property-based UUID matching (a.id), NOT Neo4j internal id(a)
    query = f"""
    MATCH (a {{id: $source_id}})-[r]-(b:{target_label})
    WITH count(DISTINCT b) as total, collect(DISTINCT b.name)[0..$limit] as examples
    RETURN total, examples
    """

    try:
        with driver.session() as session:
            record = session.run(query, source_id=source_node_id, limit=example_limit).single()
            if record:
                result = {"total": record["total"], "examples": record["examples"]}
            else:
                result = {"total": 0, "examples": []}
            logger.info(
                f"mixed_search_count found {result['total']} items for label '{target_label}' (source: {source_node_id})"
            )
            return result
    except Exception as e:
        logger.error(f"Error in mixed_search_count: {e}")
        return {"total": 0, "examples": []}


def count_all_by_label(target_topics: list[str], example_limit: int = 30) -> dict[str, Any]:
    """
    Count ALL nodes of given labels without requiring a source relationship.
    Used when context is University (i.e., count all Faculty/Major/etc. in the entire graph).
    """
    driver = connect_neo4j()

    total = 0
    all_examples = []

    try:
        with driver.session() as session:
            for target_topic in target_topics:
                target_label = target_topic
                query = f"""
                MATCH (b:{target_label})
                WITH count(DISTINCT b) as total, collect(DISTINCT b.name)[0..$limit] as examples
                RETURN total, examples
                """
                record = session.run(query, limit=example_limit).single()
                if record:
                    total += record["total"]
                    all_examples.extend(record["examples"])

                logger.info(f"count_all_by_label found {total} total items so far, after label '{target_label}'")

            return {"total": total, "examples": all_examples[:example_limit]}
    except Exception as e:
        logger.error(f"Error in count_all_by_label: {e}")
        return {"total": 0, "examples": []}
