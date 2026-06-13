"""
MongoDB helper for fetching media from activities collection.

This module provides functions to:
- Connect to MongoDB with connection pooling
- Query media URLs for given node_ids
- Random sample media for better UX
"""

import logging
import random
from typing import Any
from urllib.parse import urlparse

from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, PyMongoError

from configs.config_service import get_settings

settings = get_settings()

logger = logging.getLogger(__name__)

# Singleton MongoDB client
_mongo_client: MongoClient | None = None


def get_mongo_client() -> MongoClient:
    """
    Get or create MongoDB client singleton with connection pooling.

    Returns:
            MongoClient: Configured MongoDB client

    Raises:
            ConnectionFailure: If cannot connect to MongoDB
    """
    global _mongo_client

    if _mongo_client is None:
        try:
            uri = settings.mongo_db_uri
            max_pool_size = settings.mongodb_max_pool_size
            timeout = settings.mongodb_timeout

            _mongo_client = MongoClient(
                uri, maxPoolSize=max_pool_size, serverSelectionTimeoutMS=timeout, connectTimeoutMS=timeout
            )

            # Test connection
            _mongo_client.admin.command("ping")
            # logger.info(f"MongoDB connected: {uri}")

        except ConnectionFailure as e:
            logger.error(f"MongoDB connection failed: {e}")
            raise
        except Exception as e:
            logger.error(f"MongoDB initialization error: {e}")
            raise

    return _mongo_client


def _extract_file_info(url: str) -> dict[str, Any]:
    """
    Extract file type, name, and estimate size from URL.

    Args:
            url: Media file URL

    Returns:
            Dict with file_type, file_name, size
    """
    try:
        parsed = urlparse(url)
        from pathlib import Path

        filename = Path(parsed.path).name

        # Extract file extension
        ext = filename.split(".")[-1].upper() if "." in filename else "UNKNOWN"

        # Map common extensions to file types
        file_type_map = {
            "JPG": "JPG",
            "JPEG": "JPG",
            "PNG": "PNG",
            "GIF": "GIF",
            "WEBP": "WEBP",
            "MP4": "MP4",
            "MOV": "MOV",
            "AVI": "AVI",
            "PDF": "PDF",
        }
        file_type = file_type_map.get(ext, ext)

        return {
            "url": url,
            "file_type": file_type,
            "file_name": filename,
            "size": 0,  # Frontend can fetch actual size if needed
        }
    except Exception as e:
        logger.error(f" Failed to parse URL {url}: {e}")
        return {"url": url, "file_type": "UNKNOWN", "file_name": "unknown", "size": 0}


def fetch_media_for_entities(
    node_ids: list[str],
    min_sample: int = 2,
    max_sample: int = 5,
    randomize: bool = True,
) -> dict[str, list[dict]]:
    """
    Fetch media URLs for given node_ids from MongoDB.

    Args:
            node_ids: List of Neo4j node IDs
            min_sample: Minimum preferred number of media to sample (default: 2)
            max_sample: Maximum number of media to sample (default: 5)

    Returns:
            Dict with 'attachments' key containing list of media objects:
            {
                    "attachments": [
                            {
                                    "url": "https://...",
                                    "file_type": "PNG",
                                    "file_name": "photo.png",
                                    "size": 31650
                            }
                    ]
            }
    """
    try:
        # Get MongoDB configuration
        db_name = settings.mongo_db_name
        collection_name = settings.mongo_collection_name

        # Get client and collection
        client = get_mongo_client()
        db = client[db_name]
        collection = db[collection_name]

        logger.info(f"Fetching media for {node_ids} node_ids from {db_name}.{collection_name}")

        # Query MongoDB for documents with matching node_ids
        query = {"node_id": {"$in": node_ids}}
        projection = {"media_source_urls": 1}

        documents = list(collection.find(query, projection))
        logger.info(f"Found {len(documents)} documents with media")

        # Collect all media URLs
        all_media_urls = []
        seen_urls = set()
        for doc in documents:
            media_urls = doc.get("media_source_urls", [])
            if media_urls and isinstance(media_urls, list):
                for url in media_urls:
                    if not isinstance(url, str) or not url:
                        continue
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    all_media_urls.append(url)

        logger.info(f"Total media URLs: {len(all_media_urls)}")

        # Random sample
        if not all_media_urls:
            logger.warning(" No media URLs found")
            return {"attachments": []}

        # sample_size = max(1, min(int(max_sample or 5), len(all_media_urls)))
        # if randomize and len(all_media_urls) > sample_size:
        #     all_media_urls = random.sample(all_media_urls, sample_size)
        # else:
        #     all_media_urls = all_media_urls[:sample_size]

        if len(all_media_urls) < int(min_sample or 0):
            logger.info("Media count below min_sample=%s; returning available media only", min_sample)

        # Build attachments with metadata
        attachments = []
        for url in all_media_urls:
            if url and isinstance(url, str):  # Validate URL
                attachment = _extract_file_info(url)
                attachments.append(attachment)

        logger.info(f"Built {len(attachments)} attachments")

        return {"attachments": attachments}

    except PyMongoError as e:
        logger.error(f"MongoDB query error: {e}")
        return {"attachments": []}
    except Exception as e:
        logger.error(f"Unexpected error fetching media: {e}")
        return {"attachments": []}


def close_mongo_connection():
    """
    Close MongoDB connection and cleanup resources.
    Call this on application shutdown.
    """
    global _mongo_client

    if _mongo_client is not None:
        try:
            _mongo_client.close()
            logger.info(" MongoDB connection closed")
        except Exception as e:
            logger.error(f"Error closing MongoDB: {e}")
        finally:
            _mongo_client = None
