import json
import traceback
from typing import Any

from pymilvus import Collection, MilvusClient
from pymilvus.orm import utility

from configs.config_service import get_settings
from dbs.milvus_helper import connect_milvus, get_embedding, search_entity_by_name
from schemas.query_plan_config import ExtractedEntity, ResolvedEntity
from utils.logging_config import get_logger

settings = get_settings()

logger = get_logger(__name__)

# =============================================================================
# ENTITY RESOLVER
# ============================================================================


class EntityResolver:
    """
    Service resolve entity từ text sử dụng Milvus vector search.

    READ-ONLY behavior: this service does not create or modify data.

    Attributes
    ----------
    COLLECTION_NAME : str
            Default Milvus collection name.
    host : str
            Milvus server host.
    port : str
            Milvus server port.
    embedding_service : EmbeddingService
            Embedding generation service.
    config_path : Path
            Path to rules configuration.

    Example
    -------
    """

    def __init__(self):
        """Initialize EntityResolver from environment variables."""
        self.host = settings.milvus_host
        self.port = settings.milvus_port
        self.user = settings.milvus_user
        self.password = settings.milvus_password
        self.database = settings.milvus_database
        # self.embedding_service = EmbeddingService()
        self.collection_name = "knowledge_university_entity"
        self._client: MilvusClient | None = None
        self._rules_config: dict | None = None
        # self.config_path = Path(__file__).parent.parent / "config" / "rules.yaml"

    # =========================================================================
    # CONNECTION MANAGEMENT
    # =========================================================================

    def _get_client(self) -> MilvusClient:
        """
        Lazily initialize the Milvus client.

        Returns
        -------
        MilvusClient
                Connected Milvus client.
        """
        if self._client is None:
            uri = f"http://{self.host}:{self.port}"
            self._client = MilvusClient(uri=uri, user=self.user, password=self.password, db_name=self.database)
        return self._client

    def _load_rules(self) -> dict[str, Any]:
        """
        Load rules configuration.

        Returns
        -------
        dict
                Rules configuration containing `entity_search` settings.
        """
        if self._rules_config is None:
            self._rules_config = {}
        return self._rules_config

    # =========================================================================
    # SINGLE ENTITY RESOLUTION
    # =========================================================================

    def resolve_entity(
        self,
        entity_text: str,
        entity_search_config: dict,
        top_k: int = 1,
        score_threshold: float = 0.65,
        time_condition: dict[str, str] | None = None,
    ) -> list[Any] | ResolvedEntity | None:
        """
        Search for an entity in Milvus with node-type filtering.

        Parameters
        ----------
        entity_text : str
                Search text (for example: "Computer Science", "IT").
        entity_search_config : dict
                Config with collection and entity types::

                        {
                                "collection": "knowledge_university_entity",
                                "entity_types": ["Major", "Specialization"]
                        }
        top_k : int, default=3
                Maximum number of vector search results.
        score_threshold : float, default=0.5
                Minimum similarity score threshold (0.0 - 1.0).
        time_condition : dict, optional

        Returns
        -------
        ResolvedEntity, optional
                Resolved entity when found with score >= threshold.
                `None` if no matching entity is found.
        """
        get_embedding(texts=[entity_text])

        # client = self._get_client()

        collection_name = self.collection_name
        entity_types = entity_search_config.get("entity_types", ["Major", "Specialization"])

        # Validate collection exists
        if not connect_milvus():
            return []

        if not utility.has_collection(collection_name):
            logger.info(f"Collection '{collection_name}' does not exist")
            return []

        Collection(name=collection_name)

        # Build filter expression
        if len(entity_types) == 1:
            f'node_type == "{entity_types[0]}"'
        else:
            f"node_type in {json.dumps(entity_types)}"

        try:
            # results = collection.search(
            #     data=query_embedding,
            #     limit=top_k,
            #     expr=type_filter,
            #     output_fields=["node_id", "node_name", "node_type", "description"],
            #     param=search_params,
            #     anns_field="embedding",
            # )
            results = search_entity_by_name(
                entity_name=[entity_text],
                entity_type=entity_types,
                # expr=type_filter,
                time=time_condition,
            )

            # Process results
            # if results and len(results) > 0 and len(results[0]) > 0:
            #     best_hit = results[0]
            #     score = best_hit.get("distance", 0)
            #
            #     if score >= score_threshold:
            #         entity_data = best_hit.get("entity", {})
            entity_data = results[0]
            result = ResolvedEntity(
                entity_type=entity_data.get("node_type", "Major"),
                entity_id=entity_data.get("node_id", ""),
                entity_name=entity_data.get("node_name", entity_text),
                similarity_score=entity_data.get("score", 0.0),
                description=entity_data.get("description", ""),
            )
            logger.info(f"[EntityResolver] Entity result '{result}'")
            return result

        except Exception as e:
            traceback.print_exc()
            logger.exception("[EntityResolver] Error searching in Milvus: %s", e)

        return None

    # =========================================================================
    # BATCH ENTITY RESOLUTION
    # =========================================================================

    def resolve_entities(
        self,
        entities: list[ExtractedEntity],
        top_k: int = 1,
        score_threshold: float = 0.65,
        time_condition: dict[str, str] | None = None,
    ) -> list[ResolvedEntity]:
        """
        Resolve multiple entities in batch.

        Parameters
        ----------
        entities : list[ExtractedEntity]
                List of entities extracted by the LLM.
        top_k : int, default=3
                Maximum number of results per search.
        score_threshold : float, default=0.5
                Similarity score threshold.
        time_condition : dict, optional

        Returns
        -------
        list[ResolvedEntity]
                Successfully resolved entities.
        """
        resolved = []
        # rules_config = self._load_rules()
        # entity_search_config = rules_config.get("entity_search", {})

        for entity in entities:
            node_type = entity.label

            # Build search config
            search_config = {"collection": self.collection_name, "entity_types": [node_type]}

            if entity.label in ["Major", "Specialization"]:
                search_config["entity_types"] = ["Major", "Specialization"]

            result = self.resolve_entity(
                entity_text=entity.text,
                entity_search_config=search_config,
                top_k=top_k,
                score_threshold=score_threshold,
                time_condition=time_condition,
            )

            if isinstance(result, ResolvedEntity):
                resolved.append(result)
        logger.info(f"[EntityResolver] resolve_entities results {resolved}")
        return resolved

    # =========================================================================
    # CONVENIENCE METHODS
    # =========================================================================

    def search_entities_by_types(
        self, entity_text: str, entity_types: list[str], top_k: int = 3, score_threshold: float = 0.5
    ) -> ResolvedEntity | None:
        """
        Convenience helper to search by a list of entity types.

        Parameters
        ----------
        entity_text : str
                Search text.
        entity_types : list[str]
                Node types to filter on.
        top_k : int, default=3
                Maximum number of results.
        score_threshold : float, default=0.5
                Similarity score threshold.

        Returns
        -------
        ResolvedEntity, optional
                Resolved entity or `None`.
        """
        config = {"collection": self.collection_name, "entity_types": entity_types}
        resolved = self.resolve_entity(entity_text, config, top_k, score_threshold)
        if isinstance(resolved, ResolvedEntity):
            return resolved
        return None

    # =========================================================================
    # CLEANUP
    # =========================================================================

    def close(self):
        """Close the Milvus connection."""
        if self._client:
            self._client.close()
            self._client = None


# =============================================================================
# SINGLETON
# =============================================================================

_entity_resolver: EntityResolver | None = None


def get_entity_resolver() -> EntityResolver:
    global _entity_resolver
    if _entity_resolver is None:
        _entity_resolver = EntityResolver()
    return _entity_resolver
