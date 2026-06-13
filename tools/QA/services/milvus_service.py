"""Milvus Vector Database Service.

Wrapper around milvus_helper to provide clean abstraction layer.
"""

import logging
from typing import Any, Optional

from configs.config_service import get_settings
from dbs.milvus_helper import (
    connect_milvus,
)

from .embedding_service import get_embedding_service
from .entity_service import get_entity_service

logger = logging.getLogger(__name__)


class MilvusService:
    """Service for Milvus vector database operations."""

    _instance: Optional["MilvusService"] = None

    def __new__(cls):
        """Singleton pattern."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        """Milvus service initialized as a singleton."""
        if self._initialized:
            return

        self.embedding_service = get_embedding_service()
        self.entity_service = get_entity_service()

        # Connect to Milvus with knowledge_vector_dtb database
        milvus_db_name = get_settings().milvus_database
        if not connect_milvus(db_name=milvus_db_name):
            raise ConnectionError("Failed to connect to Milvus")

        self._initialized = True
        logger.info(" MilvusService initialized")

    @classmethod
    def _get_cache_service(cls):
        """Get cache service (lazy import to avoid circular dependency)."""
        try:
            from .cache_service import get_cache_service

            return get_cache_service()
        except Exception as e:
            logger.debug(f"Could not get cache service: {e}")
            return None

    def get_vector_collections(self) -> dict[str, Any]:
        """Get list of vector collections.

        Returns:
            Dict with collection information
        """
        try:
            from pymilvus import utility

            collections = utility.list_collections()
            return {
                "status": "success",
                "collections": collections,
                "count": len(collections),
            }
        except Exception as e:
            logger.error(f"Failed to get collections: {e}")
            return {
                "status": "error",
                "message": str(e),
            }

    def get_vector_stats(self) -> dict[str, Any]:
        """Get vector database statistics."""
        try:
            from pymilvus import Collection

            stats = {"collections": {}}

            for collection_name in ["indicator_keyword"]:
                try:
                    collection = Collection(name=collection_name)
                    stats["collections"][collection_name] = {
                        "num_entities": collection.num_entities,
                    }
                except Exception as e:
                    logger.warning(f"Could not get stats for {collection_name}: {e}")

            return {"status": "success", "stats": stats}

        except Exception as e:
            logger.error(f"Failed to get vector stats: {e}")
            return {
                "status": "error",
                "message": str(e),
            }


def get_milvus_service() -> MilvusService:
    """Get singleton MilvusService instance."""
    return MilvusService()
