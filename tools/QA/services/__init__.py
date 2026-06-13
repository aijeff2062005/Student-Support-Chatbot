"""EDU Agent Services - Centralized database and utility services."""

# import config from project root
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from .cache_service import get_cache_service

# import local services
from .embedding_service import get_embedding_service
from .entity_service import get_entity_service
from .milvus_service import get_milvus_service
from .neo4j_service import get_neo4j_service

__all__ = [
    "get_embedding_service",
    "get_cache_service",
    "get_entity_service",
    "get_milvus_service",
    "get_neo4j_service",
]
