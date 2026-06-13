"""Unified Cache Service.

Provides semantic caching with TTL support and similarity matching.
- Exact match: Returns cached result immediately (no extra API calls)
- Similarity match: Returns cached result for similar queries (>= 0.65 similarity)
- TTL expiry: Removes stale entries after configured TTL seconds

All cache operations must go through this service.
"""

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

# import config from configs directory
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
from configs.config_service import get_settings

from .embedding_service import get_embedding_service

logger = logging.getLogger(__name__)


class CacheService:
    """Semantic cache service with TTL and similarity matching.

    Design:
    - semantic_cache.json: Stores query -> result + timestamp
    - Embeddings fetched on-demand from EmbeddingService (no duplication)

    Features:
    - Exact match queries (TTL-based)
    - Similar queries (cosine similarity >= 0.5)
    - Automatic expiry of stale entries
    """

    _instance: Optional["CacheService"] = None
    _cache: dict[str, Any] = {}
    _semantic_cache_file = "semantic_cache.json"

    def __new__(cls):
        """Singleton pattern."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        """Initialize cache service."""
        if self._initialized:
            return

        settings = get_settings()
        self.embedding_service = get_embedding_service()
        self.ttl_seconds = settings.cache_ttl_seconds
        self.semantic_threshold = settings.semantic_cache_threshold

        # Load caches from files
        self._load_caches()

        self._initialized = True
        logger.info(" CacheService initialized")
        logger.info(f"TTL: {self.ttl_seconds}s")
        logger.info(f"Semantic threshold: {self.semantic_threshold}")

    def _load_caches(self):
        """Load semantic cache from file."""
        # Load semantic cache
        if os.path.exists(self._semantic_cache_file):
            try:
                with open(self._semantic_cache_file, encoding="utf-8") as f:
                    semantic_cache = json.load(f)
                    self._cache["semantic"] = semantic_cache
                    logger.info(f"Loaded {len(semantic_cache)} cached semantic entries")
            except Exception as e:
                logger.error(f"Failed to load semantic cache: {e}")
                self._cache["semantic"] = {}
        else:
            self._cache["semantic"] = {}

    def _save_caches(self):
        """Save semantic cache to file (async to avoid blocking)."""

        # Run file write in background thread to avoid blocking
        def _write_file():
            try:
                with open(self._semantic_cache_file, "w", encoding="utf-8") as f:
                    json.dump(self._cache["semantic"], f, ensure_ascii=False, indent=2)
                logger.debug(f"Semantic cache saved ({len(self._cache['semantic'])} entries)")
            except Exception as e:
                logger.error(f"Failed to save semantic cache: {e}")

        # Write in background thread (daemon=True = don't block shutdown)
        thread = threading.Thread(target=_write_file, daemon=True)
        thread.start()

    def _is_expired(self, timestamp: float) -> bool:
        """Check if cache entry is expired."""
        return time.time() - timestamp > self.ttl_seconds

    def get_semantic_cache_hit(self, query: str, query_embedding: list[float] | None = None) -> dict[str, Any] | None:
        """Check semantic cache for similar queries (>= 0.5 similarity threshold).

        Embeddings are fetched from embedding_service (not stored in semantic cache).

        Args:
            query: Query string
            query_embedding: Pre-computed embedding (optional)

        Returns:
            Dict with "from_cache": True/False and "result" if hit, else None
        """
        # Reload cache from file to pick up recent updates
        self._load_caches()

        semantic_cache = self._cache.get("semantic", {})

        if not semantic_cache:
            return None

        # Get query embedding if not provided (from embedding service cache)
        if query_embedding is None:
            try:
                query_embedding = self.embedding_service.get_embedding(query)
            except Exception as e:
                logger.error(f"Failed to get embedding for semantic cache check: {e}")
                return None

        # Check exact match first (fast path)
        if query in semantic_cache:
            entry = semantic_cache[query]
            if not self._is_expired(entry.get("timestamp", 0)):
                logger.info(" EXACT semantic cache HIT!")
                return {
                    "from_cache": True,
                    "result": entry.get("result"),
                    "match_type": "exact",
                    "similarity": 1.0,  # Exact match = 100%
                }
            else:
                del semantic_cache[query]

        # Check similarity with other cached queries
        best_match = None
        best_similarity = 0.0

        for cached_query, entry in semantic_cache.items():
            if self._is_expired(entry.get("timestamp", 0)):
                del semantic_cache[cached_query]
                continue

            # Fetch embedding for cached query from embedding service
            try:
                cached_embedding = self.embedding_service.get_embedding(cached_query)
            except Exception as e:
                logger.debug(f"Could not get embedding for cached query '{cached_query}': {e}")
                continue

            similarity = self._cosine_similarity(query_embedding, cached_embedding)

            if similarity > best_similarity:
                best_similarity = similarity
                best_match = entry

        if best_similarity >= self.semantic_threshold:
            logger.info(f"Semantic cache HIT! (similarity: {best_similarity:.4f})")
            best_result = best_match.get("result") if isinstance(best_match, dict) else None
            return {
                "from_cache": True,
                "result": best_result,
                "match_type": "similarity",
                "similarity": best_similarity,
            }

        logger.debug(f"Semantic cache MISS (best similarity: {best_similarity:.4f})")
        # Return consistent format even on miss
        return {"from_cache": False, "result": None, "match_type": "none", "similarity": 0.0}

    def set_semantic_cache(self, query: str, embedding: list[float] | None, result: dict[str, Any]):
        """Cache result for semantic matching (returns immediately, saves async).

        Embeddings are NOT stored here - they come from embedding_service.
        This avoids duplicate storage and keeps semantic_cache.json small.

        Args:
            query: Query string
            embedding: Query embedding (ignored - not stored, will fetch from embedding_service)
            result: Result to cache
        """
        # Store in memory immediately
        self._cache["semantic"][query] = {
            "result": result,
            "timestamp": time.time(),
        }
        # Save to file async (non-blocking)
        self._save_caches()
        # Return immediately - file write happens in background
        logger.debug(f"Semantic cache stored (in-memory): {query[:50]}... [file save async]")

    def _cosine_similarity(self, vec1: list[float], vec2: list[float]) -> float:
        """Calculate cosine similarity between two vectors."""
        import numpy as np

        vec1_np = np.array(vec1)
        vec2_np = np.array(vec2)

        dot_product = np.dot(vec1_np, vec2_np)
        norm1 = np.linalg.norm(vec1_np)
        norm2 = np.linalg.norm(vec2_np)

        if norm1 == 0 or norm2 == 0:
            return 0.0

        return float(dot_product / (norm1 * norm2))

    def get_cache_stats(self) -> dict[str, Any]:
        """Get cache statistics."""
        semantic_cache = self._cache.get("semantic", {})

        # Count non-expired entries
        valid_semantic = sum(1 for e in semantic_cache.values() if not self._is_expired(e.get("timestamp", 0)))

        return {
            "semantic_cache_total": len(semantic_cache),
            "semantic_cache_valid": valid_semantic,
            "ttl_seconds": self.ttl_seconds,
            "semantic_threshold": self.semantic_threshold,
        }

    def clear_cache(self):
        """Clear semantic cache."""
        self._cache["semantic"] = {}
        self._save_caches()
        logger.info(" Semantic cache cleared")


def get_cache_service() -> CacheService:
    """Get singleton CacheService instance."""
    return CacheService()
