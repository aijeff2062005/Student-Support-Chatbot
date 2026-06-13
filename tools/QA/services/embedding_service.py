"""Centralized Embedding Service.

Handles all embedding operations with caching to avoid duplicate API calls.
This is the SINGLE point of entry for all embedding requests.
All traffic routed through LiteLLM proxy.
"""

import logging
import sys
from pathlib import Path
from typing import Any, Optional

import litellm

# import config from configs directory
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
from configs.config_service import get_settings
from configs.llm_client import get_embedding_params

logger = logging.getLogger(__name__)


class EmbeddingService:
    """Centralized embedding service with in-memory caching only.

    ⚡ Optimized: No file persistence.
    - In-memory cache for current session (fast lookups)
    - On-demand fetch from API (no pre-computed embeddings)
    - Reset on server restart (fine - queries are rare)
    """

    _instance: Optional["EmbeddingService"] = None
    _embedding_cache: dict[str, list[float]] = {}

    def __new__(cls):
        """Singleton pattern."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        """Initialize embedding service (in-memory cache only)."""
        if self._initialized:
            return

        self._embedding_cache = {}
        self._initialized = True
        logger.info(" EmbeddingService initialized")
        logger.info(f"Model: {get_embedding_params()['model']}")
        logger.info("Mode: In-memory cache only (no persistence)")

    def get_embedding(self, text: str) -> list[float]:
        """Get embedding for text (with caching).

        This is the ONLY way to get embeddings in the system.
        All embedding requests must go through this method.

        Args:
            text: Text to embed

        Returns:
            List[float]: Embedding vector

        Raises:
            Exception: If embedding API fails
        """
        if not text:
            logger.warning("Empty text provided to get_embedding")
            return [0.0] * get_settings().embedding_dimension

        # Create cache key with dimensions
        dimensions = get_embedding_params()["dimensions"]
        cache_key = f"{text}_{dimensions}"

        # Check cache first
        if cache_key in self._embedding_cache:
            logger.debug(f"Embedding retrieved from cache: {text[:50]}...")
            return self._embedding_cache[cache_key]

        logger.debug(f"Computing embedding for: {text[:50]}...")

        try:
            resp = litellm.embedding(
                **get_embedding_params(),
                input=text,
            )
            embedding = resp.data[0]["embedding"]

            # Cache the embedding
            self._embedding_cache[cache_key] = embedding
            logger.debug(f"Embedding computed & cached: {text[:50]}... (dim: {len(embedding)})")
            return embedding

        except Exception as e:
            logger.error(f"Failed to get embedding: {e}")
            return [0.0] * get_settings().embedding_dimension

    def get_embedding_stats(self) -> dict[str, Any]:
        """Get embedding service statistics."""
        params = get_embedding_params()
        return {
            "cached_embeddings": len(self._embedding_cache),
            "model": params["model"],
            "dimension": params["dimensions"],
        }

    def clear_cache(self):
        """Clear embedding cache (for testing/cleanup)."""
        self._embedding_cache.clear()
        logger.info(" Embedding cache cleared")


def get_embedding_service() -> EmbeddingService:
    """Get singleton EmbeddingService instance."""
    return EmbeddingService()
