"""
Milvus Helper for EDU Agent - Real Milvus Integration with Qwen3-Embedding-8B.

Handles connection, collection management, and vector operations for EDU knowledge base.
"""

import ast
import json
import math
import re
import traceback
import unicodedata
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import litellm
from pymilvus import (
    Collection,
    CollectionSchema,
    DataType,
    FieldSchema,
    connections,
    utility,
)

from configs.config_service import get_settings
from configs.llm_client import get_embedding_params
from utils.logging_config import get_logger
from utils.parallel_web_search import run_parallel_web_search_sync

settings = get_settings()

logger = get_logger(__name__)

# Milvus Configuration
MILVUS_HOST = settings.milvus_host
MILVUS_PORT = settings.milvus_port
MILVUS_USER = settings.milvus_user
MILVUS_PASSWORD = settings.milvus_password
MILVUS_DATABASE = settings.milvus_database
EMBEDDING_DIMENSION = settings.embedding_dimension


def get_embedding(texts: list[str], dimensions: int | None = None) -> list[list[float]]:
    """
    Get embedding vectors from Qwen3-Embedding-8B via LiteLLM proxy.

    Args:
        texts: List of texts to embed
        dimensions:

    Returns:
        List[List[float]]: List of embedding vectors
    """
    try:
        _cfg = get_embedding_params()
        _cfg["dimensions"] = dimensions if dimensions is not None else EMBEDDING_DIMENSION

        response = litellm.embedding(
            **_cfg,
            input=texts,
        )
        embeddings = [item["embedding"] for item in response.data]

        # Truncate if needed (safety net in case API returns more dims than requested)
        dimensions = get_settings().embedding_dimension if dimensions is None else dimensions
        if embeddings and len(embeddings[0]) > dimensions:
            logger.warning(f"API returned {len(embeddings[0])} dims, truncating to {dimensions}")
            embeddings = [emb[:dimensions] for emb in embeddings]

        return embeddings

    except Exception as e:
        logger.error(f"Embedding API error: {e}")
        dimensions = get_settings().embedding_dimension
        return [[0.0] * dimensions for _ in range(len(texts))]


def connect_milvus(db_name: str = MILVUS_DATABASE) -> bool:
    """
    Connect to Milvus server using environment variables.

    Args:
        db_name: Database name to connect to (default: "default")

    Returns:
        bool: True if connection successful
    """
    try:
        # Check if already connected
        if connections.has_connection("default"):
            return True

        logger.info(f"Connecting to Milvus: {MILVUS_HOST}:{MILVUS_PORT}, DB: {db_name}")

        connections.connect(
            alias="default",
            host=MILVUS_HOST,
            port=MILVUS_PORT,
            user=MILVUS_USER,
            password=MILVUS_PASSWORD,
            db_name=db_name,
        )

        return True

    except Exception as e:
        logger.error(f"Milvus connection error: {e}")
        return False


def search_similar_documents(
    collection_name: str,
    query: list[str] | None = None,
    expr: str | None = None,
    query_vector: list[list[float]] | None = None,
    output_fields: list[str] | None = None,
    top_k: int = 1,
    threshold: float = 0.7,
    anns_field: str = "embedding",
) -> list[dict[str, Any]]:
    """
    Search for similar documents in Milvus collection.

    Args:
        collection_name: Name of the collection to search
        query: Query text (optional if query_vector provided)
        top_k: Number of results to return
        threshold: Minimum similarity score
        expr: Filter by document type (optional)
        query_vector: Pre-computed embedding vector (optional)
        anns_field: Vector field name (default: "embedding")
        output_fields: Fields to retrieve (default: ["content", "source", "doc_type", "created_at"])

    Returns:
        List of dicts containing search results
    """
    try:
        if not connect_milvus():
            return []

        if not utility.has_collection(collection_name):
            logger.info(f"Collection '{collection_name}' does not exist")
            return []

        collection = Collection(name=collection_name)

        # Get query embedding
        if query_vector is not None:
            query_embeddings = query_vector
        elif query:
            query_embeddings = get_embedding(query)
        else:
            return []

        if all(v == 0.0 for v in query_embeddings):
            logger.error("Failed to get query embedding")
            return []

        # Prepare search parameters
        search_params = {"metric_type": "COSINE", "params": {"ef": 200}}

        # Filter expression
        expr = expr if expr else None
        # Default output fields if not provided
        if output_fields is None:
            output_fields = ["content", "source", "doc_type", "created_at"]

        # Search
        results = collection.search(
            data=query_embeddings,
            anns_field=anns_field,
            param=search_params,
            limit=top_k,
            expr=expr,
            output_fields=output_fields,
        )

        # Filter by threshold and format results
        filtered_results = []
        for hits in results:
            for hit in hits:
                if hit.score >= threshold:
                    # Create result dict dynamically based on output_fields
                    result_item = {
                        "id": str(hit.id) if hasattr(hit, "id") else "",
                        "score": float(hit.score),
                    }

                    # Add requested fields
                    for field in output_fields:
                        # Handle wildcard or specific fields
                        result_item[field] = hit.entity.get(field, "")

                    filtered_results.append(result_item)

        # Sort by score descending
        filtered_results.sort(key=lambda x: x["score"], reverse=True)

        return filtered_results

    except Exception as e:
        traceback.print_exc()
        logger.error(f"Search error: {e}")
        return []


async def semantic_resolve_attributes(
    requested_attributes: list[str],
    available_attribute_keys: list[str],
    similarity_threshold: float = 0.6,
    top_k: int = 1,
    collection_name: str = "knowledge_university_entity_property",
) -> list[dict]:

    if not requested_attributes or not available_attribute_keys:
        logger.warning("Empty inputs, fallback to description")
        return []

    try:
        # ============================================
        # STEP 1: Check for EXACT matches first
        # ============================================
        exact_matches = []
        non_exact_requests = []
        available_set = set(attr.lower() for attr in available_attribute_keys)
        available_original = {attr.lower(): attr for attr in available_attribute_keys}

        for req_attr in requested_attributes:
            req_lower = req_attr.lower()
            if req_lower in available_set:
                # Exact match found - add with score 1.0
                original_key = available_original[req_lower]
                exact_matches.append({"attribute_key": original_key, "score": 1.0})
            else:
                non_exact_requests.append(req_attr)

        # ============================================
        # STEP 2: Semantic search for non-exact matches
        # ============================================
        semantic_matches = []

        if non_exact_requests:
            query_embeddings = get_embedding(non_exact_requests)

            if not connect_milvus():
                return exact_matches if exact_matches else []

            if not utility.has_collection(collection_name):
                logger.info(f"Collection '{collection_name}' does not exist")
                return exact_matches if exact_matches else []

            collection = Collection(name=collection_name)
            import json

            # Convert to list in case it's a RepeatedScalarContainer from protobuf
            available_keys_list = list(available_attribute_keys)
            filter_expr = f"property_key in {json.dumps(available_keys_list)}"

            search_params = {"metric_type": "COSINE", "params": {"ef": 200}}

            search_results = collection.search(
                data=query_embeddings,
                anns_field="embedding",
                param=search_params,
                limit=top_k,
                expr=filter_expr,
                output_fields=["property_key", "labels", "usage_count"],
            )

            for hits in search_results:
                for hit in hits:
                    if hit.score >= similarity_threshold:
                        result_item = {
                            "attribute_key": hit.entity.get("property_key"),
                            "score": float(hit.score),
                        }
                        semantic_matches.append(result_item)

        # ============================================
        # STEP 3: Combine and deduplicate results
        # ============================================
        all_matches = exact_matches + semantic_matches

        # Deduplicate - keep highest score for each attribute
        deduped_results = {}
        for result in all_matches:
            attr_key = result["attribute_key"]
            if attr_key not in deduped_results or result["score"] > deduped_results[attr_key]["score"]:
                deduped_results[attr_key] = result

        # Sort by score descending
        final_results = sorted(deduped_results.values(), key=lambda x: x["score"], reverse=True)

        # Fallback if no matches
        if not final_results:
            logger.warning("No matches found, fallback to ['description', 'name']")
            return []

        return final_results

    except Exception as e:
        logger.error(f"Semantic resolution error: {e}", exc_info=True)
        return []


def search_chunks_by_query(
    query: list[str],
    node_id: int | None = None,
    similarity_threshold: float = 0.7,
    top_k: int = 5,
) -> list[dict[str, Any]]:
    """
    Search chunks using semantic similarity in chunks_knowledge_university_entity collection.
    """
    try:
        # Get embedding for query
        query_embedding = get_embedding(query)

        # Build filter expression
        expr = None
        if node_id is not None:
            expr = f"node_id == {node_id}"

        # Search in chunks_knowledge_university_entity collection
        results = search_similar_documents(
            collection_name="chunks_knowledge_university_entity",
            query_vector=query_embedding,
            top_k=top_k,
            threshold=similarity_threshold,
            expr=expr,
            anns_field="embedding",
            output_fields=["id", "node_id", "title_chunk", "content"],
        )

        if not results:
            logger.warning("No chunks found for query")
            return []

        # Format results
        formatted_results = []
        for r in results:
            formatted_results.append(
                {
                    "chunk_id": r.get("id", ""),
                    "node_id": r.get("node_id"),
                    "title_chunk": r.get("title_chunk", ""),
                    "content": r.get("content", ""),
                    "similarity_score": float(r.get("score", 0.0)),
                }
            )

        logger.info(f"Found {len(formatted_results)} chunks")
        return formatted_results

    except Exception as e:
        logger.error(f"Error searching chunks: {e}")
        return []


def _collect_entity_search_terms(
    entity_name: list[Any] | None,
    keywords: list[str] | None = None,
    keyword_attributes: list[str] | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """Normalize entity/query payloads and collect keyword constraints."""

    def _as_clean_list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            cleaned = value.strip()
            return [cleaned] if cleaned else []
        if isinstance(value, (list, tuple, set)):
            cleaned_items = []
            for item in value:
                cleaned_item = str(item).strip()
                if cleaned_item:
                    cleaned_items.append(cleaned_item)
            return cleaned_items
        return []

    def _dedupe(values: list[str]) -> list[str]:
        deduped: list[str] = []
        seen: set[str] = set()
        for value in values:
            normalized = value.strip()
            if not normalized:
                continue
            key = normalized.casefold()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(normalized)
        return deduped

    def _extract_from_dict(payload: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
        name_candidates: list[str] = []
        for key in ("entity_name", "name", "node_name", "text", "query", "original_query"):
            name_candidates.extend(_as_clean_list(payload.get(key)))
        kw_candidates = _as_clean_list(payload.get("keywords"))
        kw_attr_candidates = _as_clean_list(payload.get("keyword_attributes"))
        return name_candidates, kw_candidates, kw_attr_candidates

    query_terms: list[str] = []
    keyword_terms: list[str] = []
    keyword_attr_terms: list[str] = []

    for item in entity_name or []:
        if isinstance(item, dict):
            names, kws, kw_attrs = _extract_from_dict(item)
            query_terms.extend(names)
            keyword_terms.extend(kws)
            keyword_attr_terms.extend(kw_attrs)
            continue

        raw_item = str(item).strip()
        if not raw_item:
            continue

        parsed_payload: Any = None
        if raw_item[0] in {"{", "["}:
            try:
                parsed_payload = json.loads(raw_item)
            except Exception:
                parsed_payload = None

        if isinstance(parsed_payload, dict):
            names, kws, kw_attrs = _extract_from_dict(parsed_payload)
            query_terms.extend(names)
            keyword_terms.extend(kws)
            keyword_attr_terms.extend(kw_attrs)
            continue
        if isinstance(parsed_payload, list):
            for payload_item in parsed_payload:
                if isinstance(payload_item, dict):
                    names, kws, kw_attrs = _extract_from_dict(payload_item)
                    query_terms.extend(names)
                    keyword_terms.extend(kws)
                    keyword_attr_terms.extend(kw_attrs)
                elif isinstance(payload_item, str):
                    cleaned_payload_item = payload_item.strip()
                    if cleaned_payload_item:
                        query_terms.append(cleaned_payload_item)
            continue

        query_terms.append(raw_item)

    query_terms = _dedupe(query_terms)
    keyword_terms = _dedupe(keyword_terms + _as_clean_list(keywords))
    keyword_attr_terms = _dedupe(keyword_attr_terms + _as_clean_list(keyword_attributes))

    combined_keywords = _dedupe(keyword_terms + keyword_attr_terms)
    if not query_terms:
        query_terms = [", ".join(combined_keywords)] if combined_keywords else [""]

    query_for_full_text = query_terms if query_terms else ""
    return query_terms, combined_keywords, query_for_full_text


def search_entity_by_name(
    entity_name: list[str],
    entity_type: str | list[str] | None = None,
    top_k: int = 1,
    threshold: float = 0.70,
    expr: str | None = None,
    time: dict[str, str] | None = None,
    keywords: list[str] | None = None,
    keyword_attributes: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Search entity by name using semantic similarity."""
    try:
        logger.info(f"Searching entity: '{entity_name}' (type filter: {entity_type})")
        query_terms, keyword_terms, query_for_full_text = _collect_entity_search_terms(
            entity_name=entity_name,
            keywords=keywords,
            keyword_attributes=keyword_attributes,
        )
        normalized_entity_types = [entity_type] if isinstance(entity_type, str) else entity_type
        is_course_search = bool(normalized_entity_types) and set(normalized_entity_types) == {"Course"}

        expr_time = ""
        if time and not is_course_search:
            raw_from_year = time.get("from_year")
            raw_to_year = time.get("to_year", "")
            from_year = str(raw_from_year) if raw_from_year not in (None, "") else ""
            to_year = str(raw_to_year) if raw_to_year not in (None, "") else ""
            logger.debug("milvus search_entity_by_name time filter: from_year=%s, to_year=%s", from_year, to_year)
            if to_year and from_year:
                to_year = str(int(to_year) + 1)
            if from_year and to_year:
                expr_time = f"(effective_from > '{from_year}' || effective_from == '') && (effective_to < '{to_year}'  || effective_to == '')"
            elif from_year:
                expr_time = f"effective_from > '{from_year}'  || effective_from == ''"
            elif to_year:
                current_date = datetime.now().strftime("%Y-%m-%d")
                expr_time = f"effective_to >= '{current_date}' || effective_to == ''"
            else:
                expr_time = None
        active_expr = "is_active == true"
        expr_extend = ""

        if normalized_entity_types:
            if len(normalized_entity_types) == 1:
                expr_extend = f'node_type == "{normalized_entity_types[0]}"'
                if normalized_entity_types[0] == "AcademicProgram":
                    query_terms = [f"[CHƯƠNG TRÌNH ĐÀO TẠO] {query_terms[0]}"]
                if normalized_entity_types[0] in {"Major", "Specialization"}:
                    expr_extend = "node_type in ['Major', 'Specialization']"
                    threshold = 0.8
                if normalized_entity_types[0] == "Faculty":
                    query_name = query_terms[0]
                    if "Khoa" not in query_name and "khoa" not in query_name:
                        query_terms = [f"Khoa {query_name}"]
                    threshold = 0.85
                if normalized_entity_types[0] == "AdmissionMethod":
                    threshold = 0.6
                if normalized_entity_types[0] == "Person":
                    threshold = 0.85
            else:
                import json

                if "Major" in normalized_entity_types or "Specialization" in normalized_entity_types:
                    normalized_entity_types = list(set(normalized_entity_types) | {"Major", "Specialization"})
                expr = f"node_type in {json.dumps(normalized_entity_types)}"
                if "AcademicProgram" in normalized_entity_types:
                    query_terms = [f"[CHƯƠNG TRÌNH ĐÀO TẠO] {query_terms[0]}"]

        if expr:
            expr = f"({expr}) and ({active_expr})"
        else:
            expr = active_expr
        if expr_extend:
            expr = f"({expr}) and ({expr_extend})"
        if expr_time:
            expr = f"({expr}) and ({expr_time})"
        logger.info(f"[search_entity_by_name] Milvus expr: {expr}")

        entity_embedding = get_embedding(query_terms)
        logger.info(f"Got entity embedding for query terms: {query_terms}")
        rerank_pool_k = min(top_k, 5)

        formatted_results: list[dict[str, Any]] = []
        try:
            results = search_similar_documents(
                collection_name="knowledge_university_entity",
                query_vector=entity_embedding,
                top_k=rerank_pool_k,
                expr=expr,
                threshold=threshold,
                anns_field="embedding",
                output_fields=[
                    "node_id",
                    "node_name",
                    "node_type",
                    "attribute_keys",
                    "description",
                    # "keywords",
                ],
            )
            # logger.info(f"Len primary entity search returned {len(results)}")
            logger.info(f"Primary entity search returned {results} results before formatting and reranking")

            for r in results:
                attribute_keys = r.get("attribute_keys", [])

                # Force convert protobuf to list
                if hasattr(attribute_keys, "__iter__") and not isinstance(attribute_keys, (str, dict)):
                    attribute_keys = list(attribute_keys)  # Force to Python list

                formatted_results.append(
                    {
                        "node_id": str(r.get("node_id", "")),  # Ensure string
                        "node_name": str(r.get("node_name", "")),
                        "node_type": str(r.get("node_type", "")),
                        "attribute_keys": attribute_keys,  # Now clean
                        "description": str(r.get("description", "")),
                        # "keywords": list(r.get("keywords", []) or []),
                        "score": float(r.get("score", 0.0)),
                    }
                )

            if formatted_results:
                formatted_results = _rerank_entity_results_by_full_text(
                    query_entity_name=query_for_full_text,
                    candidates=formatted_results,
                    limit=top_k,
                )
        except Exception as primary_search_error:
            logger.warning("Primary entity search/rerank failed, will try keyword fallback: %s", primary_search_error)
            formatted_results = []

        if formatted_results:
            logger.info(f"Found {len(formatted_results)} entities with score {formatted_results[0]['score']}")
            return_limit = top_k * len(query_for_full_text) if len(query_for_full_text) > 1 else top_k
            return formatted_results[:return_limit]

        # Final independent fallback: keyword-only vector search.
        if keyword_terms:
            logger.info("Primary entity search empty/unavailable, running keyword fallback search")
            keyword_query = ", ".join(keyword_terms).lower()
            keyword_embedding = get_embedding([keyword_query], dimensions=2048)
            keyword_results = search_similar_documents(
                collection_name="knowledge_university_entity",
                query_vector=keyword_embedding,
                top_k=rerank_pool_k,
                expr=expr,
                threshold=max(0.35, threshold - 0.4),
                anns_field="keywords_embedding",
                output_fields=[
                    "node_id",
                    "node_name",
                    "node_type",
                    "attribute_keys",
                    "description",
                    "keywords",
                ],
            )
            fallback_formatted_results: list[dict[str, Any]] = []
            for r in keyword_results:
                attribute_keys = r.get("attribute_keys", [])
                if hasattr(attribute_keys, "__iter__") and not isinstance(attribute_keys, (str, dict)):
                    attribute_keys = list(attribute_keys)
                fallback_formatted_results.append(
                    {
                        "node_id": str(r.get("node_id", "")),
                        "node_name": str(r.get("node_name", "")),
                        "node_type": str(r.get("node_type", "")),
                        "attribute_keys": attribute_keys,
                        "description": str(r.get("description", "")),
                        "keywords": list(r.get("keywords", []) or []),
                        "score": float(r.get("score", 0.0)),
                    }
                )
            formatted_results = fallback_formatted_results[:top_k]

        if formatted_results:
            logger.info(f"Found {len(formatted_results)} entities with score {formatted_results[0]['score']}")
        else:
            logger.info("Found 0 entities after primary search and keyword fallback")
        return formatted_results

    except Exception as e:
        logger.error(f"Error searching entity: {e}")
        traceback.print_exc()
        return []


def get_nodes_by_type(node_types: str | list[str], limit: int = 1) -> list[dict[str, Any]]:
    """
    Get nodes of specific type(s) from knowledge_university_entity collection.
    Returns exactly 1 node per type (or fewer if not available).

    Args:
        node_types: Single node type (str) or multiple node types (List[str])
                   Examples: "Major" or ["Major", "Faculty", "Campus"]
        limit: Maximum number of nodes per type (default: 1)

    Returns:
        Flat list with 1 node per type, each containing:
        - attribute_keys: List of available attribute keys for that type

    Examples:
        # Single type
        nodes = get_nodes_by_type("Major", limit=1)
        # Returns: [{"attribute_keys": ["name", "tuition_fee", ...]}]

        # Multiple types
        nodes = get_nodes_by_type(["Major", "Faculty", "Campus"], limit=1)
        # Returns: [
        #   {"attribute_keys": ["name", "tuition_fee", ...]},  # Major
        #   {"attribute_keys": ["name", "dean", ...]},         # Faculty
        #   {"attribute_keys": ["name", "location", ...]}      # Campus
        # ]
    """
    try:
        # Normalize to list for consistent handling
        if isinstance(node_types, str):
            node_types = [node_types]

        logger.info(f"Getting {limit} node(s) per type for: {node_types}")

        if not connect_milvus():
            return []

        if not utility.has_collection("knowledge_university_entity"):
            logger.info("Collection 'knowledge_university_entity' does not exist")
            return []

        collection = Collection(name="knowledge_university_entity")

        # Build filter expression
        if len(node_types) == 1:
            # Single type - use equality operator
            expr = f'node_type == "{node_types[0]}"'
        else:
            # Multiple types - use IN operator
            import json

            expr = f"node_type in {json.dumps(node_types)}"

        # Use dummy vector search with type filter
        dummy_vector = [0.0] * EMBEDDING_DIMENSION

        search_params = {"metric_type": "COSINE", "params": {"ef": 2000}}

        # Query with higher limit to ensure coverage of all types
        # Then post-process to get exactly `limit` nodes per type
        query_limit = len(node_types) * limit * 10  # Safety margin

        results = collection.search(
            data=[dummy_vector],
            anns_field="embedding",
            param=search_params,
            limit=query_limit,
            expr=expr,
            output_fields=["node_type", "attribute_keys"],
        )

        # Step: Group results by node_type and take first `limit` of each
        type_groups = {}
        for hits in results:
            for hit in hits:
                node_type = hit.entity.get("node_type")
                if node_type not in type_groups:
                    type_groups[node_type] = []

                # Only add up to `limit` nodes per type
                if len(type_groups[node_type]) < limit:
                    type_groups[node_type].append(
                        {
                            "attribute_keys": hit.entity.get("attribute_keys", []),
                        }
                    )

        # Flatten results while preserving order of input node_types
        formatted_results = []
        for node_type in node_types:
            if node_type in type_groups:
                formatted_results.extend(type_groups[node_type])
            else:
                logger.warning(f" No nodes found for type: {node_type}")
        return formatted_results

    except Exception as e:
        logger.error(f"Error getting nodes by type: {e}", exc_info=True)
        return []


def search_school_by_name_like(
    high_school: str,
    high_school_province: str,
    high_school_address: str | None = None,
    db_name: str = "search_school_name",
    collection_name: str = "search_school_name",
    limit: int = 5,
) -> list[dict[str, Any]]:
    try:
        # Step 1: Create a separate connection with unique alias for this database
        # This allows maintaining multiple connections to different databases simultaneously
        connection_alias = f"conn_{db_name}"

        if not connections.has_connection(connection_alias):
            connections.connect(
                alias=connection_alias,
                host=MILVUS_HOST,
                port=MILVUS_PORT,
                user=MILVUS_USER,
                password=MILVUS_PASSWORD,
                db_name=db_name,
            )
            logger.info(f"Created new connection '{connection_alias}' to database: {db_name}")

        # Step 2: Check if collection exists (use the specific connection)
        if not utility.has_collection(collection_name, using=connection_alias):
            logger.info(f"Collection '{collection_name}' does not exist in database '{db_name}'")
            return []

        collection = Collection(name=collection_name, using=connection_alias)
        collection.load()

        # Step 3: Build LIKE filter expression
        # Escape special characters in search terms
        school_name_escaped = high_school.replace('"', '\\"')
        province_escaped = high_school_province.replace('"', '\\"')

        # Build filter: school_name like "%keyword%" && province like "%keyword%"
        filter_conditions = [
            f'school_name like "%{school_name_escaped}%"',
            f'province == "{province_escaped}"',
        ]

        # Add address condition if provided
        if high_school_address:
            address_escaped = high_school_address.replace('"', '\\"')
            filter_conditions.append(f'address like "%{address_escaped}%"')

        filter_expr = " && ".join(filter_conditions)

        logger.info(f"Searching schools with filter: {filter_expr}")

        # Step 4: Query collection (no vector search, just filter)
        # Output fields: all except embedding
        output_fields = ["id", "school_name", "province", "address"]

        results = collection.query(expr=filter_expr, output_fields=output_fields, limit=limit)

        # Step 5: Format and return results
        formatted_results = []
        for item in results:
            formatted_results.append(
                {
                    "id": str(item.get("id", "")),
                    "school_name": str(item.get("school_name", "")),
                    "province": str(item.get("province", "")),
                    "address": str(item.get("address", "")),
                }
            )

        logger.info(f"Found {len(formatted_results)} schools matching criteria")
        return formatted_results

    except Exception as e:
        logger.error(f"Error searching school by name: {e}")
        traceback.print_exc()
        return []


def search_school_hybrid(
    high_school: str,
    high_school_province: str,
    high_school_address: str | None = None,
    db_name: str = "search_school_name",
    collection_name: str = "search_school_name",
    limit: int = 3,
    vector_threshold: float = 0.8,
) -> list[dict[str, Any]]:
    """
    Search for schools using vector search with province LIKE filter.

    - School name: always vector search (embedding)
    - Province: LIKE filter only (not included in embedding)

    Args:
        high_school: School name to search
        high_school_province: Province/City name (used as LIKE filter)
        high_school_address: Optional address
        db_name: Database name (default: "search_school_name")
        collection_name: Collection name (default: "search_school_name")
        limit: Maximum results to return (default: 5)
        vector_threshold: Similarity threshold for vector search (default: 0.96)

    Returns:
        List of matching schools with id, school_name, province, address
    """
    try:
        # Create connection for this database
        connection_alias = f"conn_{db_name}"

        if not connections.has_connection(connection_alias):
            connections.connect(
                alias=connection_alias,
                host=MILVUS_HOST,
                port=MILVUS_PORT,
                user=MILVUS_USER,
                password=MILVUS_PASSWORD,
                db_name=db_name,
            )

        if not utility.has_collection(collection_name, using=connection_alias):
            logger.info(f"Collection '{collection_name}' does not exist in database '{db_name}'")
            return []

        collection = Collection(name=collection_name, using=connection_alias)
        collection.load()

        # Get embedding for school name ONLY (province is filter, not part of embedding)
        query_text = f"{high_school}"
        query_embeddings = get_embedding([query_text], dimensions=4096)

        if not query_embeddings or all(v == 0.0 for v in query_embeddings[0]):
            logger.error("Failed to get query embedding for vector search")
            return []

        # Build province LIKE filter
        province_escaped = high_school_province.replace('"', '\\"')
        filter_expr = f'province like "%{province_escaped}%"'

        # Search parameters
        search_params = {"metric_type": "COSINE", "params": {"ef": 200}}

        output_fields = ["id", "school_name", "province", "address"]

        results = collection.search(
            data=query_embeddings,
            anns_field="embedding",
            param=search_params,
            limit=limit,
            expr=filter_expr,
            output_fields=output_fields,
        )

        # Filter by threshold and format results
        formatted_results = []
        for hits in results:
            for hit in hits:
                if hit.score >= vector_threshold:
                    formatted_results.append(
                        {
                            "id": str(hit.entity.get("id", "")),
                            "school_name": str(hit.entity.get("school_name", "")),
                            "province": str(hit.entity.get("province", "")),
                            "address": str(hit.entity.get("address", "")),
                            "score": float(hit.score),
                        }
                    )
        if formatted_results:
            formatted_results = _rerank_school_results_by_full_text(
                query_school_name=high_school,
                candidates=formatted_results,
                limit=limit,
            )

        if formatted_results:
            logger.info(f"Vector search found {len(formatted_results)} results with score >= {vector_threshold}")
            return formatted_results

        logger.info("Vector search found no results, falling back to LIKE search")

        # Step 2: Fallback to LIKE search if vector search fails
        return search_school_by_name_like(
            high_school=high_school,
            high_school_province=high_school_province,
            high_school_address=high_school_address,
            db_name=db_name,
            collection_name=collection_name,
            limit=limit,
        )

    except Exception as e:
        logger.error(f"Error in school search: {e}")
        traceback.print_exc()
        return []


def get_node_name_by_id(node_id: str | int) -> str | None:
    """
    Get node_name by node_id from knowledge_university_entity collection.

    Args:
        node_id: ID of the node

    Returns:
        str: node_name or None if not found
    """
    try:
        if not connect_milvus():
            return None

        if not utility.has_collection("knowledge_university_entity"):
            logger.info("Collection 'knowledge_university_entity' does not exist")
            return None

        collection = Collection(name="knowledge_university_entity")

        # Assume node_id is int or string representing int
        expr = f'node_id == "{node_id}"'

        results = collection.query(expr=expr, output_fields=["node_name"], limit=1)

        if results:
            return str(results[0].get("node_name", ""))

        return None

    except Exception as e:
        logger.error(f"Error getting node name by id: {e}")
        return None


def ensure_crawled_data_collection(
    collection_name: str = "knowledge_university_crawled_data",
) -> bool:
    """
    Ensure FAQ collection exists. If not, create it with proper schema and index.

    Args:
        collection_name: Name of the FAQ collection (default: "knowledge_university_crawled_data")

    Returns:
        bool: True if collection exists or was created successfully
    """
    try:
        if not connect_milvus():
            return False

        # Check if collection already exists
        if utility.has_collection(collection_name):
            logger.info(f"Collection '{collection_name}' already exists")
            return True

        logger.info(f"Creating collection '{collection_name}'...")

        # Define schema
        fields = [
            FieldSchema(name="id", dtype=DataType.VARCHAR, is_primary=True, max_length=36),
            FieldSchema(name="question", dtype=DataType.VARCHAR, max_length=1000),
            FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=EMBEDDING_DIMENSION),
            FieldSchema(name="content", dtype=DataType.VARCHAR, max_length=5000),
            FieldSchema(name="created_at", dtype=DataType.INT64),
            FieldSchema(name="updated_at", dtype=DataType.INT64),
            FieldSchema(
                name="source_type",
                dtype=DataType.ARRAY,
                element_type=DataType.VARCHAR,
                max_capacity=10,
                max_length=200,
            ),
            FieldSchema(
                name="source_url",
                dtype=DataType.ARRAY,
                element_type=DataType.VARCHAR,
                max_capacity=20,
                max_length=500,
            ),
            FieldSchema(
                name="supporting_node_type",
                dtype=DataType.ARRAY,
                element_type=DataType.VARCHAR,
                max_capacity=10,
                max_length=100,
            ),
            FieldSchema(name="status", dtype=DataType.VARCHAR, max_length=50),
        ]

        schema = CollectionSchema(fields=fields, description="FAQ Collection for University Knowledge")

        # Create collection
        collection = Collection(name=collection_name, schema=schema)
        logger.info(f"Collection '{collection_name}' created successfully")

        # Create HNSW index for embedding field
        index_params = {
            "metric_type": "COSINE",
            "index_type": "HNSW",
            "params": {"M": 16, "efConstruction": 200},
        }

        logger.info("Creating HNSW index on 'embedding' field...")
        collection.create_index(field_name="embedding", index_params=index_params)
        logger.info(" Index created successfully")

        # Load collection into memory
        collection.load()
        logger.info(" Collection loaded into memory")

        return True

    except Exception as e:
        logger.error(f"Error ensuring FAQ collection: {e}")
        traceback.print_exc()
        return False


def search_attribute_definitions(
    keywords: list[str],
    label: str | None = None,
    collection_name: str = "knowledge_attribute_definitions",
    search_field: str = "embedding_attribute_keywords",
    similarity_threshold: float = 0.5,
    top_k: int = 5,
) -> list[dict[str, Any]]:
    """
    Resolve semantic keywords to actual attribute definitions via vector search.

    Joins the keyword list into a single text, embeds it, and searches the
    `knowledge_attribute_definitions` collection on `embedding_attribute_keywords`.
    Returns matched attribute definitions sorted by similarity (descending).

    This bridges the semantic keyword_attributes (from query_agent) to the actual
    schema attribute names used by downstream search functions.

    Args:
        keywords: Semantic keywords extracted from user query (e.g. keyword_attributes).
        label: Optional filter by entity type (e.g. "Major", "AcademicProgram").
        collection_name: Milvus collection to search.
        search_field: Vector field to search against.
        similarity_threshold: Minimum cosine similarity score.
        top_k: Maximum number of results to return.

    Returns:
        List of dicts with keys: label, attribute_name, attribute_description,
        attribute_keywords, similarity_score.
    """
    if not keywords:
        logger.warning("search_attribute_definitions: empty keywords, skipping")
        return []

    try:
        if not connect_milvus():
            return []

        if not utility.has_collection(collection_name):
            logger.warning(f"Collection '{collection_name}' does not exist, skipping attribute definition search")
            return []

        collection = Collection(name=collection_name)
        collection.load()

        # Join all keywords into a single search text for embedding
        joined_keywords = ", ".join(keywords)
        logger.info(f"search_attribute_definitions: searching with keywords: {joined_keywords}")

        query_embedding = get_embedding([joined_keywords])
        if not query_embedding:
            return []

        search_params = {"metric_type": "COSINE", "params": {"ef": 200}}

        # Build optional label filter
        filter_expression = f'label == "{label}"' if label else None

        output_fields = [
            "label",
            "attribute_name",
            "attribute_description",
            "attribute_keywords",
        ]

        results = collection.search(
            data=query_embedding,
            anns_field=search_field,
            param=search_params,
            limit=top_k,
            expr=filter_expression,
            output_fields=output_fields,
        )

        matched_definitions: list[dict[str, Any]] = []

        if not results or not results[0]:
            logger.info("search_attribute_definitions: no results from Milvus")
            return matched_definitions

        for hit in results[0]:
            similarity_score = hit.score

            if similarity_score < similarity_threshold:
                continue

            entity = hit.entity
            matched_definitions.append(
                {
                    "label": entity.get("label", ""),
                    "attribute_name": entity.get("attribute_name", ""),
                    "attribute_description": entity.get("attribute_description", ""),
                    "attribute_keywords": entity.get("attribute_keywords", []),
                    "similarity_score": float(similarity_score),
                }
            )

        matched_definitions.sort(key=lambda x: x["similarity_score"], reverse=True)

        logger.info(
            "search_attribute_definitions: resolved definitions: %s",
            [
                {
                    "label": item.get("label", ""),
                    "attribute_name": item.get("attribute_name", ""),
                    "similarity_score": round(float(item.get("similarity_score", 0.0)), 6),
                }
                for item in matched_definitions
            ],
        )

        logger.info(
            f"search_attribute_definitions: found {len(matched_definitions)} "
            f"definitions above threshold {similarity_threshold}"
        )

        return matched_definitions

    except Exception as e:
        logger.error(f"Error in search_attribute_definitions: {e}")
        traceback.print_exc()
        return []


def search_entity_attributes_hybrid(
    node_ids: list[str],
    queries: list[str],
    top_k: int = 1,
    similarity_threshold: float = 0.6,
    collection_name: str = "knowledge_university_entity_attribute",
    expr: str | None = None,
    node_ids_batch_size: int = 200,
    prefer_exact_name_query: bool = False,
) -> list[dict[str, Any]]:
    """
    Step 2: Search entity attributes via Milvus (Hybrid Strategy).
    1. Search by Attribute Name (embedding_name)
    2. If no result, Search by Attribute Value (embedding_value)

    If node_ids is provided, search is performed in `IN (...)` batches for efficiency
    while preserving per-node fallback logic (name first, value fallback for missing nodes).
    Optional fast path:
      - when `prefer_exact_name_query=True`, do scalar query on `attribute_name in [...]`
        (and scoped node_ids) before semantic vector search.
    """
    try:
        logger.info(
            "search_entity_attributes_hybrid INPUT: node_ids_count=%d, queries=%s, top_k=%d, "
            "similarity_threshold=%.3f, collection=%s, expr=%s, node_ids_batch_size=%d, prefer_exact_name_query=%s",
            len(node_ids or []),
            queries,
            top_k,
            similarity_threshold,
            collection_name,
            expr,
            node_ids_batch_size,
            prefer_exact_name_query,
        )

        # Require queries
        if not queries:
            logger.warning("search_entity_attributes_hybrid OUTPUT: empty queries -> []")
            return []

        if not connect_milvus():
            logger.error("search_entity_attributes_hybrid OUTPUT: Milvus connection failed -> []")
            return []

        if not utility.has_collection(collection_name):
            logger.info(f"Collection '{collection_name}' does not exist")
            return []

        collection = Collection(name=collection_name)
        collection.load()

        max_limit_per_query = 16384

        def _quote_expr_string(value: str) -> str:
            return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'

        def _build_node_in_expr(target_node_ids: list[str]) -> str:
            return "node_id in [" + ",".join(_quote_expr_string(n) for n in target_node_ids) + "]"

        def _build_attr_name_in_expr(attr_names: list[str]) -> str:
            return "attribute_name in [" + ",".join(_quote_expr_string(a) for a in attr_names) + "]"

        def _cap_per_node(
            candidates: list[dict[str, Any]],
            scoped_node_ids: list[str] | None,
            per_node_top_k: int,
        ) -> list[dict[str, Any]]:
            if not candidates:
                return []

            if not scoped_node_ids:
                return candidates

            candidates = sorted(candidates, key=lambda x: x["score"], reverse=True)
            grouped: dict[str, list[dict[str, Any]]] = {}
            for c in candidates:
                nid = c.get("node_id")
                if nid is None:
                    continue
                bucket = grouped.setdefault(nid, [])
                if len(bucket) < per_node_top_k:
                    bucket.append(c)

            flattened: list[dict[str, Any]] = []
            for nid in scoped_node_ids:
                flattened.extend(grouped.get(nid, []))
            return flattened

        # Fast path: exact attribute_name scalar query (very fast for resolved attr names)
        if prefer_exact_name_query and queries:
            clean_attr_names = list(dict.fromkeys([str(q).strip() for q in queries if str(q).strip()]))
            if clean_attr_names:
                logger.info(
                    "search_entity_attributes_hybrid: exact-name fast path enabled, attr_names=%s",
                    clean_attr_names,
                )
                exact_results: list[dict[str, Any]] = []
                if node_ids:
                    deduped_node_ids = list(dict.fromkeys(node_ids))
                    batch_size = max(1, int(node_ids_batch_size))
                    node_id_batches = [
                        deduped_node_ids[i : i + batch_size] for i in range(0, len(deduped_node_ids), batch_size)
                    ]
                    attr_expr = _build_attr_name_in_expr(clean_attr_names)
                    for batch_idx, batch_ids in enumerate(node_id_batches, start=1):
                        node_expr = _build_node_in_expr(batch_ids)
                        combined_expr = f"({node_expr}) and ({attr_expr})"
                        if expr:
                            combined_expr = f"({expr}) and ({combined_expr})"
                        limit = min(max(top_k * len(batch_ids), top_k), max_limit_per_query)
                        rows = collection.query(
                            expr=combined_expr,
                            output_fields=["node_id", "attribute_name", "attribute_value"],
                            limit=limit,
                        )
                        batch_candidates = [
                            {
                                "node_id": row.get("node_id"),
                                "attribute_name": row.get("attribute_name"),
                                "attribute_value": row.get("attribute_value"),
                                "score": 1.0,
                                "match_type": "name_exact",
                            }
                            for row in rows
                        ]
                        batch_candidates = _cap_per_node(
                            batch_candidates,
                            scoped_node_ids=batch_ids,
                            per_node_top_k=top_k,
                        )
                        logger.info(
                            "search_entity_attributes_hybrid: exact-name batch %d/%d -> %d results",
                            batch_idx,
                            len(node_id_batches),
                            len(batch_candidates),
                        )
                        exact_results.extend(batch_candidates)
                else:
                    attr_expr = _build_attr_name_in_expr(clean_attr_names)
                    combined_expr = f"({expr}) and ({attr_expr})" if expr else attr_expr
                    rows = collection.query(
                        expr=combined_expr,
                        output_fields=["node_id", "attribute_name", "attribute_value"],
                        limit=min(max(top_k, 1), max_limit_per_query),
                    )
                    exact_results = [
                        {
                            "node_id": row.get("node_id"),
                            "attribute_name": row.get("attribute_name"),
                            "attribute_value": row.get("attribute_value"),
                            "score": 1.0,
                            "match_type": "name_exact",
                        }
                        for row in rows
                    ]

                if exact_results:
                    logger.info(
                        "search_entity_attributes_hybrid OUTPUT: exact-name fast path hit, total_results=%d, sample=%s",
                        len(exact_results),
                        exact_results[:3],
                    )
                    return exact_results
                logger.info(
                    "search_entity_attributes_hybrid: exact-name fast path found no rows, fallback to semantic search"
                )

        # Generate embedding for the queries (Batch)
        query_embeddings = get_embedding(queries)
        if not query_embeddings:
            logger.warning("search_entity_attributes_hybrid OUTPUT: embedding empty -> []")
            return []
        logger.info(
            "search_entity_attributes_hybrid: generated %d query embeddings",
            len(query_embeddings),
        )

        search_params = {
            "metric_type": "COSINE",
            "params": {"M": 16, "efConstruction": 200},
        }

        def _collect_candidates(search_results, match_type: str) -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            for hits in search_results:
                for hit in hits:
                    if hit.score >= similarity_threshold:
                        out.append(
                            {
                                "node_id": hit.entity.get("node_id"),
                                "attribute_name": hit.entity.get("attribute_name"),
                                "attribute_value": hit.entity.get("attribute_value"),
                                "score": hit.score,
                                "match_type": match_type,
                            }
                        )
            return out

        # Keep stable ordering for global path as before
        def _sort_if_global(candidates: list[dict[str, Any]], scoped_node_ids: list[str] | None):
            if not scoped_node_ids:
                candidates.sort(key=lambda x: x["score"], reverse=True)
            return candidates

        # Helper to perform search for a specific filter
        def _execute_search(
            current_expr: str | None,
            limit_per_search: int,
            scoped_node_ids: list[str] | None = None,
        ):
            if limit_per_search > max_limit_per_query:
                logger.info(
                    "search_entity_attributes_hybrid: limit_per_search=%d exceeds cap=%d, truncating",
                    limit_per_search,
                    max_limit_per_query,
                )
                limit_per_search = max_limit_per_query

            # Strategy 1: Search by Attribute Name
            logger.info(f"Step 2.1: Search by Attribute Name (expr={current_expr})")
            results_name = collection.search(
                data=query_embeddings,
                anns_field="embedding_name",
                param=search_params,
                limit=limit_per_search,
                expr=current_expr,
                output_fields=["node_id", "attribute_name", "attribute_value"],
            )
            name_candidates = _cap_per_node(
                _collect_candidates(results_name, "name"),
                scoped_node_ids=scoped_node_ids,
                per_node_top_k=top_k,
            )
            name_candidates = _sort_if_global(name_candidates, scoped_node_ids)

            if scoped_node_ids:
                nodes_with_name = {c["node_id"] for c in name_candidates if c.get("node_id") is not None}
                missing_nodes = [nid for nid in scoped_node_ids if nid not in nodes_with_name]
            else:
                missing_nodes = []

            if name_candidates:
                logger.info(
                    "search_entity_attributes_hybrid: name-match candidates=%d (expr=%s, missing_nodes=%d)",
                    len(name_candidates),
                    current_expr,
                    len(missing_nodes),
                )
                if not scoped_node_ids:
                    return name_candidates
                if not missing_nodes:
                    return name_candidates

            # Strategy 2: Search by Attribute Value (fallback)
            if scoped_node_ids:
                target_nodes = missing_nodes if name_candidates else scoped_node_ids
                value_node_expr = _build_node_in_expr(target_nodes)
                value_expr = f"({current_expr}) and ({value_node_expr})" if current_expr else value_node_expr
                value_limit = max(top_k * max(1, len(target_nodes)), top_k)
                value_limit = min(value_limit, max_limit_per_query)
            else:
                value_expr = current_expr
                value_limit = limit_per_search

            logger.info(f"Step 2.2: Search by Attribute Value (expr={value_expr})")
            results_value = collection.search(
                data=query_embeddings,
                anns_field="embedding_value",
                param=search_params,
                limit=value_limit,
                expr=value_expr,
                output_fields=["node_id", "attribute_name", "attribute_value"],
            )

            value_candidates = _cap_per_node(
                _collect_candidates(results_value, "value"),
                scoped_node_ids=(missing_nodes if scoped_node_ids else None),
                per_node_top_k=top_k,
            )
            value_candidates = _sort_if_global(value_candidates, scoped_node_ids)

            if scoped_node_ids:
                # Keep old semantics per node:
                # node has name-match -> keep name results
                # node missing name-match -> use value fallback
                by_node_name: dict[str, list[dict[str, Any]]] = {}
                for c in name_candidates:
                    by_node_name.setdefault(c.get("node_id"), []).append(c)
                by_node_value: dict[str, list[dict[str, Any]]] = {}
                for c in value_candidates:
                    by_node_value.setdefault(c.get("node_id"), []).append(c)

                merged: list[dict[str, Any]] = []
                for nid in scoped_node_ids:
                    if by_node_name.get(nid):
                        merged.extend(by_node_name[nid][:top_k])
                    elif by_node_value.get(nid):
                        merged.extend(by_node_value[nid][:top_k])
                candidates = merged
            else:
                candidates = value_candidates

            if candidates:
                logger.info(
                    "search_entity_attributes_hybrid: value-match candidates=%d (expr=%s)",
                    len(candidates),
                    value_expr,
                )
            else:
                logger.info(
                    "search_entity_attributes_hybrid: no candidates in both name/value (expr=%s)",
                    value_expr,
                )

            return candidates

        all_results = []

        # If node_ids provided, search in IN-batches (fewer Milvus round-trips)
        if node_ids:
            deduped_node_ids = list(dict.fromkeys(node_ids))
            if not deduped_node_ids:
                # to prevent accidental global search across all universities
                logger.warning("search_entity_attributes_hybrid: node_ids resolved to empty after dedup  returning []")
                return []
            batch_size = max(1, int(node_ids_batch_size))
            node_id_batches = [
                deduped_node_ids[i : i + batch_size] for i in range(0, len(deduped_node_ids), batch_size)
            ]

            for batch_idx, batch_ids in enumerate(node_id_batches, start=1):
                batch_node_expr = _build_node_in_expr(batch_ids)
                node_specific_expr = f"({expr}) and ({batch_node_expr})" if expr else batch_node_expr
                batch_limit = max(top_k * max(1, len(batch_ids)), top_k)
                batch_limit = min(batch_limit, max_limit_per_query)
                logger.info(
                    "search_entity_attributes_hybrid: searching batch %d/%d (batch_nodes=%d, limit=%d)",
                    batch_idx,
                    len(node_id_batches),
                    len(batch_ids),
                    batch_limit,
                )
                batch_results = _execute_search(
                    node_specific_expr,
                    batch_limit,
                    scoped_node_ids=batch_ids,
                )
                logger.info(
                    "search_entity_attributes_hybrid: batch %d/%d -> %d results",
                    batch_idx,
                    len(node_id_batches),
                    len(batch_results),
                )
                all_results.extend(batch_results)
        else:
            # Global search (no node_ids restriction or expr handle externally if needed)
            logger.info("Searching attributes globally (no node_ids)")
            all_results = _execute_search(expr, node_ids_batch_size, scoped_node_ids=None)

        # Deduplicate if necessary?
        # Usually distinct searches per node won't overlap unless node_ids has duplicates.
        logger.info(
            "search_entity_attributes_hybrid OUTPUT: total_results=%d, sample=%s",
            len(all_results),
            all_results[:3],
        )
        return all_results

    except Exception as e:
        logger.error(f"Error in search_entity_attributes_hybrid: {e}")
        traceback.print_exc()
        return []


def search_relation_attributes_hybrid(
    node_ids: list[str],
    queries: list[str],
    top_k: int = 1,
    similarity_threshold: float = 0.6,
    collection_name: str = "knowledge_university_relation",
    expr: str | None = None,
) -> list[dict[str, Any]]:
    """
    Step 3: Search relation attributes via Milvus (Hybrid Strategy).
    1. Search by Relation Attribute Name (embedding_attribute)
    2. If no result, Search by Relation Attribute Value (embedding_content)

    If node_ids is provided, search is performed for EACH node_id.
    """
    try:
        if not queries:
            return []

        if not connect_milvus():
            return []

        if not utility.has_collection(collection_name):
            logger.info(f"Collection '{collection_name}' does not exist")
            return []

        collection = Collection(name=collection_name)
        collection.load()

        # Generate embedding (Batch)
        query_embeddings = get_embedding(queries)
        if not query_embeddings:
            return []

        search_params = {
            "metric_type": "COSINE",
            "params": {"M": 16, "efConstruction": 200},
        }
        logger.info(f"relation_attr_search queries: {queries}")

        def _execute_relation_search(current_expr, limit_per_search):
            candidates = []

            # Strategy 1: Search by Attribute Name (embedding_attribute)
            # logger.info(f"Step 3.1: Relation Attribute Search (expr={current_expr})")
            results_attr = collection.search(
                data=query_embeddings,
                anns_field="embedding_attribute",
                param=search_params,
                limit=limit_per_search,
                expr=current_expr,
                output_fields=[
                    "source_node_id",
                    "destination_node_id",
                    "relation_name",
                    "attribute",
                    "attribute_value",
                ],
            )

            for hits in results_attr:
                for hit in hits:
                    if hit.score >= similarity_threshold:
                        candidates.append(
                            {
                                "source_node_id": hit.entity.get("source_node_id"),
                                "destination_node_id": hit.entity.get("destination_node_id"),
                                "relation_name": hit.entity.get("relation_name"),
                                "attribute_name": hit.entity.get("attribute"),
                                "attribute_value": hit.entity.get("attribute_value"),
                                "score": hit.score,
                                "match_type": "attribute_name",
                            }
                        )

            if candidates:
                logger.info("Searching relations globally (no node_ids) with attribute name match")
                candidates.sort(key=lambda x: x["score"], reverse=True)
                return candidates

            # Strategy 2: Search by Attribute Value (embedding_content)
            # logger.info(f"Step 3.2: Relation Value Search (expr={current_expr})")
            results_content = collection.search(
                data=query_embeddings,
                anns_field="embedding_content",
                param=search_params,
                limit=limit_per_search,
                expr=current_expr,
                output_fields=[
                    "source_node_id",
                    "destination_node_id",
                    "relation_name",
                    "attribute",
                    "attribute_value",
                ],
            )

            for hits in results_content:
                for hit in hits:
                    if hit.score >= similarity_threshold:
                        candidates.append(
                            {
                                "source_node_id": hit.entity.get("source_node_id"),
                                "destination_node_id": hit.entity.get("destination_node_id"),
                                "relation_name": hit.entity.get("relation_name"),
                                "attribute_name": hit.entity.get("attribute"),
                                "attribute_value": hit.entity.get("attribute_value"),
                                "score": hit.score,
                                "match_type": "attribute_value",
                            }
                        )

            if candidates:
                logger.info("Searching relations globally (no node_ids) with attribute value match")
                candidates.sort(key=lambda x: x["score"], reverse=True)

            return candidates

        all_results = []

        if node_ids:
            for nid in node_ids:
                # Filter either source or destination
                nid_expr = f'(source_node_id == "{nid}") or (destination_node_id == "{nid}")'

                if expr:
                    node_specific_expr = f"({expr}) and ({nid_expr})"
                else:
                    node_specific_expr = nid_expr

                logger.info(f"Searching relations for node {nid}")
                node_results = _execute_relation_search(node_specific_expr, top_k)
                all_results.extend(node_results)
        else:
            logger.info("Searching relations globally (no node_ids)")
            all_results = _execute_relation_search(expr, top_k)

        return all_results

    except Exception as e:
        logger.error(f"Error in search_relation_attributes_hybrid: {e}")
        traceback.print_exc()
        return []


def search_crawled_data(
    question: str,
    top_k: int = 3,
    threshold: float = 0.7,
    collection_name: str = "knowledge_university_crawled_data",
) -> list[dict[str, Any]]:
    """
    Search for similar FAQs based on question similarity.

    Args:
        question: Question to search for
        top_k: Maximum number of results to return
        threshold: Minimum similarity score (0.0-1.0)
        collection_name: Name of the FAQ collection

    Returns:
        List of dicts containing: content, score, source_url, status
    """
    try:
        if not question:
            return []

        if not connect_milvus():
            return []

        if not utility.has_collection(collection_name):
            logger.info(f"Collection '{collection_name}' does not exist")
            return []

        collection = Collection(name=collection_name)
        collection.load()

        # Get embedding for queries (Batch)
        query_embeddings = get_embedding([question])

        if not query_embeddings:
            logger.error("Failed to get query embeddings")
            return []

        # Search parameters
        search_params = {"metric_type": "COSINE", "params": {"ef": 200}}

        # Perform search
        results = collection.search(
            data=query_embeddings,
            anns_field="embedding",
            param=search_params,
            limit=top_k,
            output_fields=["content", "source_url", "status"],
        )

        # Filter and format results
        formatted_results = []
        for hits in results:
            for hit in hits:
                if hit.score >= threshold:
                    formatted_results.append(
                        {
                            "content": str(hit.entity.get("content", "")),
                            "score": float(hit.score),
                            "source_url": list(hit.entity.get("source_url", [])),
                            "status": str(hit.entity.get("status", "")),
                        }
                    )

        # Sort by score descending
        formatted_results.sort(key=lambda x: x["score"], reverse=True)

        logger.info(f"Found {len(formatted_results)} FAQs matching query")
        return formatted_results

    except Exception as e:
        logger.error(f"Error searching FAQ: {e}")
        traceback.print_exc()
        return []


def search_common_qa(
    question: str,
    top_k: int = 3,
    threshold: float = 0.7,
    min_final_score: float = 0.0,
    collection_name: str = "knowledge_common_qa",
) -> list[dict[str, Any]]:
    """
    Search common QA entries using the schema of knowledge_common_qa.

    Schema:
      - id
      - embedding
      - question
      - answer
      - data_lv1 / data_lv2
      - created_by / updated_by
      - created_at / updated_at
      - status
    """
    try:
        if not question:
            return []

        if not connect_milvus():
            return []

        if not utility.has_collection(collection_name):
            logger.info(f"Collection '{collection_name}' does not exist")
            return []

        collection = Collection(name=collection_name)
        collection.load()

        expr = 'status == "OFFICIAL"'
        hybrid_results = full_text_search_common_qa(
            query=question,
            collection=collection,
            top_k=top_k,
            candidate_k=max(top_k * 10, 20),
            lexical_pool_limit=3000,
            lexical_top_k=300,
            similarity_threshold=threshold,
            semantic_weight=0.7,
            bm25_weight=0.2,
            lexical_weight=0.1,
            min_lexical_signal=0.02,
            semantic_rescue_threshold=max(threshold, 0.7),
            min_final_score=min_final_score,
            expr=expr,
        )

        formatted_results = []
        for item in hybrid_results:
            formatted_results.append(
                {
                    "question": str(item.get("question", "")),
                    "answer": item.get("answer", ""),
                    "raw_answer": str(item.get("raw_answer", "") or ""),
                    "data_lv1": item.get("data_lv1"),
                    "data_lv2": item.get("data_lv2"),
                    "answer_source": str(item.get("answer_source", "answer") or "answer"),
                    "score": float(item.get("semantic_raw", 0.0)),
                    "status": str(item.get("status", "")),
                    "created_at": int(item.get("created_at", 0) or 0),
                    "updated_at": int(item.get("updated_at", 0) or 0),
                    "created_by": str(item.get("created_by", "") or ""),
                    "updated_by": str(item.get("updated_by", "") or ""),
                    "semantic_raw": float(item.get("semantic_raw", 0.0)),
                    "semantic_score": float(item.get("semantic_score", 0.0)),
                    "bm25_score": float(item.get("bm25_score", 0.0)),
                    "lexical_score": float(item.get("lexical_score", 0.0)),
                    "final_score": float(item.get("final_score", 0.0)),
                }
            )

        logger.info(f"Found {len(formatted_results)} common QA items matching query")
        return formatted_results

    except Exception as e:
        logger.error(f"Error searching common QA: {e}")
        traceback.print_exc()
        return []


def insert_crawled_data(
    question: str,
    content: str,
    source_type: list[str] | None = None,
    source_url: list[str] | None = None,
    supporting_node_type: list[str] | None = None,
    status: str = "provisional",
    collection_name: str = "knowledge_university_crawled_data",
) -> bool:
    """
    Insert a new FAQ into the collection.

    Args:
        question: The question text
        content: The answer content
        source_type: Optional list of source types (e.g., ["website", "pdf"])
        source_url: Optional list of source URLs
        supporting_node_type: Optional list of supporting node types
        status: Status of the FAQ (default: "active")
        collection_name: Name of the FAQ collection

    Returns:
        bool: True if insert successful
    """
    try:
        if not connect_milvus():
            return False

        if not utility.has_collection(collection_name):
            logger.info(f"Collection '{collection_name}' does not exist. Create it first using ensure_faq_collection()")
            ensure_crawled_data_collection()
            return False

        collection = Collection(name=collection_name)

        # Generate UUID for id
        faq_id = str(uuid.uuid4())

        # Get embedding for question
        question_embedding = get_embedding([question])

        if not question_embedding or all(v == 0.0 for v in question_embedding[0]):
            logger.error("Failed to get question embedding")
            return False

        # Get current UTC timestamp
        current_timestamp = int(datetime.now(UTC).timestamp())

        # Prepare data entity
        entity = {
            "id": faq_id,
            "question": question,
            "embedding": question_embedding[0],
            "content": content,
            "created_at": current_timestamp,
            "updated_at": current_timestamp,
            "source_type": source_type if source_type else [],
            "source_url": source_url if source_url else [],
            "supporting_node_type": supporting_node_type if supporting_node_type else [],
            "status": status,
        }

        # Insert into collection
        collection.insert([entity])
        logger.info(f"FAQ inserted successfully with id: {faq_id}")

        return True

    except Exception as e:
        logger.error(f"Error inserting FAQ: {e}")
        traceback.print_exc()
        return False


# Ported from tests/full_text_search.py into production module.

_FTS_TOKEN_RE = re.compile(r"\w+", re.UNICODE)

_FTS_RELATION_OUTPUT_FIELDS = [
    "source_node_id",
    "destination_node_id",
    "relation_name",
    "attribute",
    "attribute_value",
]


@dataclass
class _RelationCandidate:
    source_node_id: str
    destination_node_id: str
    relation_name: str
    attribute: str
    attribute_value: str
    semantic_attr_raw: float = 0.0
    semantic_value_raw: float = 0.0
    semantic_score: float = 0.0
    bm25_score: float = 0.0
    lexical_score: float = 0.0
    final_score: float = 0.0


def _fts_fold_text(text: str) -> str:
    """Lowercase + strip diacritics (Vietnamese-aware)."""
    if not text:
        return ""
    text = text.lower().replace("đ", "d")
    return "".join(ch for ch in unicodedata.normalize("NFD", text) if unicodedata.category(ch) != "Mn")


def _fts_tokenize(text: str) -> list[str]:
    return _FTS_TOKEN_RE.findall(_fts_fold_text(text))


def _fts_lexical_overlap(query: str, document: str) -> float:
    """Jaccard-like token overlap: |query ∩ doc| / |query|."""
    query_tokens = set(_fts_tokenize(query))
    if not query_tokens:
        return 0.0
    doc_tokens = set(_fts_tokenize(document))
    if not doc_tokens:
        return 0.0
    return len(query_tokens & doc_tokens) / len(query_tokens)


def _fts_normalize_scores(scores: list[float]) -> list[float]:
    """Relative min-max normalization for ranking / tie-break only."""
    if not scores:
        return []
    mn, mx = min(scores), max(scores)
    if math.isclose(mn, mx):
        return [0.0 if math.isclose(mx, 0.0) else 0.5] * len(scores)
    return [(s - mn) / (mx - mn) for s in scores]


def _fts_clip_unit_interval(score: float) -> float:
    """Clamp a score into [0, 1] for absolute-confidence fusion."""
    return max(0.0, min(float(score), 1.0))


def _fts_bm25_to_confidence(score: float, pivot: float = 1.0) -> float:
    """
    Convert unbounded BM25 into a bounded confidence score in [0, 1).

    This keeps `final_score` meaningful for thresholding even when there is only
    one candidate (where relative min-max normalization would collapse to 0.5).
    """
    score = max(float(score), 0.0)
    if score <= 0.0:
        return 0.0
    return score / (score + pivot)


def _fts_bm25_scores(query: str, documents: list[str], k1: float = 1.5, b: float = 0.75) -> list[float]:
    """Okapi BM25 scores for a list of documents."""
    query_tokens = _fts_tokenize(query)
    if not query_tokens or not documents:
        return [0.0] * len(documents)

    tokenized_docs = [_fts_tokenize(doc) for doc in documents]
    total_docs = len(tokenized_docs)
    avg_doc_len = max(sum(len(d) for d in tokenized_docs) / max(total_docs, 1), 1.0)

    doc_freq: dict[str, int] = {}
    for doc in tokenized_docs:
        for token in set(doc):
            doc_freq[token] = doc_freq.get(token, 0) + 1

    idf: dict[str, float] = {
        token: math.log(1.0 + (total_docs - doc_freq.get(token, 0) + 0.5) / (doc_freq.get(token, 0) + 0.5))
        for token in set(query_tokens)
    }

    scores: list[float] = []
    for doc in tokenized_docs:
        tf = Counter(doc)
        doc_len = max(len(doc), 1)
        score = 0.0
        for token in query_tokens:
            freq = tf.get(token, 0)
            if freq <= 0:
                continue
            denom = freq + k1 * (1.0 - b + b * (doc_len / avg_doc_len))
            score += idf[token] * ((freq * (k1 + 1.0)) / denom)
        scores.append(score)
    return scores


_SCHOOL_NAME_FTS_STOPWORDS = {
    "th",
    "thcs",
    "thpt",
    "ptth",
    "truong",
    "trung",
    "hoc",
    "pho",
    "thong",
    "cap",
}


def _school_name_distinctive_tokens(text: str) -> list[str]:
    tokens = _fts_tokenize(text)
    if not tokens:
        return []

    filtered = [token for token in tokens if token not in _SCHOOL_NAME_FTS_STOPWORDS]
    base_tokens = filtered or tokens

    deduped: list[str] = []
    seen = set()
    for token in base_tokens:
        if token not in seen:
            deduped.append(token)
            seen.add(token)
    return deduped


def _rerank_school_results_by_full_text(
    query_school_name: str,
    candidates: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    """
    Re-rank/filter vector search results using lexical matching on `school_name`.

    Safe mode:
    - Keep exact normalized matches immediately.
    - Prefer candidates covering all distinctive query tokens.
    - Fall back to the original vector results if lexical evidence is too weak.
    """
    if not candidates:
        return []

    clean_query = (query_school_name or "").strip()
    if not clean_query:
        return sorted(candidates, key=lambda item: item.get("score", 0.0), reverse=True)[:limit]

    query_norm = " ".join(_fts_tokenize(clean_query))
    query_tokens = _school_name_distinctive_tokens(clean_query)
    query_token_set = set(query_tokens)

    if not query_norm or not query_token_set:
        return sorted(candidates, key=lambda item: item.get("score", 0.0), reverse=True)[:limit]

    exact_matches: list[dict[str, Any]] = []
    enriched_candidates: list[dict[str, Any]] = []
    school_name_docs: list[str] = []

    for item in candidates:
        school_name = str(item.get("school_name", "")).strip()
        normalized_name = " ".join(_fts_tokenize(school_name))

        if normalized_name and normalized_name == query_norm:
            exact_matches.append(item)
            continue

        distinctive_tokens = _school_name_distinctive_tokens(school_name)
        doc_text = " ".join(distinctive_tokens)
        matched_tokens = query_token_set & set(distinctive_tokens)

        enriched_item = dict(item)
        enriched_item["_matched_token_count"] = len(matched_tokens)
        enriched_item["_doc_text"] = doc_text
        enriched_item["_normalized_name"] = normalized_name
        enriched_candidates.append(enriched_item)
        school_name_docs.append(doc_text)

    if exact_matches:
        exact_matches.sort(key=lambda item: item.get("score", 0.0), reverse=True)
        logger.info(
            "School FTS exact normalized match for '%s': %d/%d candidates kept",
            clean_query,
            len(exact_matches),
            len(candidates),
        )
        return exact_matches[:limit]

    if not enriched_candidates:
        return sorted(candidates, key=lambda item: item.get("score", 0.0), reverse=True)[:limit]

    query_doc = " ".join(query_tokens)
    bm25_raw = _fts_bm25_scores(query_doc, school_name_docs)
    lexical_raw = [_fts_lexical_overlap(query_doc, doc) for doc in school_name_docs]

    for idx, item in enumerate(enriched_candidates):
        item["_bm25_conf"] = _fts_bm25_to_confidence(bm25_raw[idx])
        item["_lexical_overlap"] = lexical_raw[idx]
        item["_fts_score"] = 0.75 * item["_lexical_overlap"] + 0.25 * item["_bm25_conf"]

    full_coverage_matches = [
        item for item in enriched_candidates if item["_matched_token_count"] == len(query_token_set)
    ]
    if full_coverage_matches:
        selected = full_coverage_matches
        reason = "full coverage"
    else:
        min_match_count = 1 if len(query_token_set) <= 1 else 2
        min_overlap = 1.0 if len(query_token_set) == 1 else 0.6
        strong_matches = [
            item
            for item in enriched_candidates
            if item["_matched_token_count"] >= min_match_count and item["_lexical_overlap"] >= min_overlap
        ]

        if strong_matches:
            selected = strong_matches
            reason = "strong lexical signal"
        else:
            fallback_results = sorted(candidates, key=lambda item: item.get("score", 0.0), reverse=True)[:limit]
            logger.info(
                "School FTS found no strong lexical signal for '%s'; keeping original vector ranking (%d candidates)",
                clean_query,
                len(fallback_results),
            )
            return fallback_results

    selected.sort(
        key=lambda item: (
            item.get("_fts_score", 0.0),
            item.get("_matched_token_count", 0),
            item.get("_lexical_overlap", 0.0),
            item.get("score", 0.0),
        ),
        reverse=True,
    )

    output: list[dict[str, Any]] = []
    for item in selected[:limit]:
        clean_item = dict(item)
        clean_item.pop("_matched_token_count", None)
        clean_item.pop("_doc_text", None)
        clean_item.pop("_normalized_name", None)
        clean_item.pop("_bm25_conf", None)
        clean_item.pop("_lexical_overlap", None)
        clean_item.pop("_fts_score", None)
        output.append(clean_item)

    logger.info(
        "School FTS %s for '%s': %d/%d candidates kept",
        reason,
        clean_query,
        len(output),
        len(candidates),
    )
    return output


def _rerank_entity_results_by_full_text(
    query_entity_name: str | list[str],
    candidates: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    """
    Re-rank Milvus entity candidates using lexical/full-text evidence on node_name.

    This is intentionally conservative:
    - keep exact normalized matches immediately
    - otherwise prefer candidates with strong token coverage / overlap
    - fall back to original vector ranking when lexical evidence is weak
    """
    if not candidates:
        return []

    if isinstance(query_entity_name, list):
        output: list[dict[str, Any]] = []
        seen_node_ids: set[str] = set()
        query_terms = [str(query).strip() for query in query_entity_name if str(query).strip()]

        for query_term in query_terms:
            ranked_for_term = _rerank_entity_results_by_full_text(
                query_entity_name=query_term,
                candidates=candidates,
                limit=len(candidates),
            )
            selected_for_term = 0
            for item in ranked_for_term:
                node_id = item.get("node_id", "")
                if node_id in seen_node_ids:
                    continue
                seen_node_ids.add(node_id)
                output.append(item)
                selected_for_term += 1
                if selected_for_term >= limit:
                    break

        if output:
            logger.info(
                "Entity FTS reranked %d query terms: %d/%d candidates kept",
                len(query_terms),
                len(output),
                len(candidates),
            )
            return output[: limit * len(query_terms)]

        return sorted(candidates, key=lambda item: item.get("score", 0.0), reverse=True)[:limit]

    clean_query = (query_entity_name or "").strip()
    if not clean_query:
        return sorted(candidates, key=lambda item: item.get("score", 0.0), reverse=True)[:limit]

    query_norm = " ".join(_fts_tokenize(clean_query))
    query_tokens = _fts_tokenize(clean_query)
    query_token_set = set(query_tokens)

    if not query_norm or not query_token_set:
        return sorted(candidates, key=lambda item: item.get("score", 0.0), reverse=True)[:limit]

    exact_matches: list[dict[str, Any]] = []
    enriched_candidates: list[dict[str, Any]] = []
    docs: list[str] = []

    for item in candidates:
        node_name = str(item.get("node_name", "")).strip()
        normalized_name = " ".join(_fts_tokenize(node_name))

        if normalized_name and normalized_name == query_norm:
            exact_matches.append(item)
            continue

        doc_tokens = _fts_tokenize(node_name)
        doc_token_set = set(doc_tokens)
        matched_tokens = query_token_set & doc_token_set
        doc_text = " ".join(doc_tokens)

        enriched_item = dict(item)
        enriched_item["_matched_token_count"] = len(matched_tokens)
        enriched_item["_normalized_name"] = normalized_name
        enriched_item["_doc_text"] = doc_text
        enriched_candidates.append(enriched_item)
        docs.append(doc_text)

    if exact_matches:
        exact_matches.sort(key=lambda item: item.get("score", 0.0), reverse=True)
        logger.info(
            "Entity FTS exact normalized match for '%s': %d/%d candidates kept",
            clean_query,
            len(exact_matches),
            len(candidates),
        )
        return exact_matches[:limit]

    if not enriched_candidates:
        return sorted(candidates, key=lambda item: item.get("score", 0.0), reverse=True)[:limit]

    query_doc = " ".join(query_tokens)
    bm25_raw = _fts_bm25_scores(query_doc, docs)
    lexical_raw = [_fts_lexical_overlap(query_doc, doc) for doc in docs]

    for idx, item in enumerate(enriched_candidates):
        item["_bm25_conf"] = _fts_bm25_to_confidence(bm25_raw[idx])
        item["_lexical_overlap"] = lexical_raw[idx]
        item["_fts_score"] = 0.75 * item["_lexical_overlap"] + 0.25 * item["_bm25_conf"]

    full_coverage_matches = [
        item for item in enriched_candidates if item["_matched_token_count"] == len(query_token_set)
    ]
    if full_coverage_matches:
        selected = full_coverage_matches
        reason = "full coverage"
    else:
        min_match_count = 1 if len(query_token_set) <= 1 else 2
        min_overlap = 1.0 if len(query_token_set) == 1 else 0.6
        strong_matches = [
            item
            for item in enriched_candidates
            if item["_matched_token_count"] >= min_match_count and item["_lexical_overlap"] >= min_overlap
        ]
        if strong_matches:
            selected = strong_matches
            reason = "strong lexical signal"
        else:
            fallback_results = sorted(candidates, key=lambda item: item.get("score", 0.0), reverse=True)[:limit]
            logger.info(
                "Entity FTS found no strong lexical signal for '%s'; keeping original vector ranking (%d candidates)",
                clean_query,
                len(fallback_results),
            )
            return fallback_results

    selected.sort(
        key=lambda item: (
            item.get("_fts_score", 0.0),
            item.get("_matched_token_count", 0),
            item.get("_lexical_overlap", 0.0),
            item.get("score", 0.0),
        ),
        reverse=True,
    )

    output: list[dict[str, Any]] = []
    for item in selected[:limit]:
        clean_item = dict(item)
        clean_item.pop("_matched_token_count", None)
        clean_item.pop("_normalized_name", None)
        clean_item.pop("_doc_text", None)
        clean_item.pop("_bm25_conf", None)
        clean_item.pop("_lexical_overlap", None)
        clean_item.pop("_fts_score", None)
        output.append(clean_item)

    logger.info(
        "Entity FTS %s for '%s': %d/%d candidates kept",
        reason,
        clean_query,
        len(output),
        len(candidates),
    )
    return output


def _fts_search_one_field(
    collection: Collection,
    query_embedding: list[list[float]],
    anns_field: str,
    limit: int,
    expr: str | None = None,
) -> list[Any]:
    """Vector search on a single embedding field."""
    search_params = {"metric_type": "COSINE", "params": {"ef": 200}}
    try:
        result = collection.search(
            data=query_embedding,
            anns_field=anns_field,
            param=search_params,
            limit=limit,
            expr=expr,
            output_fields=_FTS_RELATION_OUTPUT_FIELDS,
        )
        return result[0] if result else []
    except Exception as exc:
        logger.exception("FTS search failed on field '%s': %s", anns_field, exc)
        return []


def _fts_fetch_lexical_pool(
    collection: Collection,
    limit: int,
    expr: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch rows for lexical scoring without relying on vector candidates."""
    try:
        query_expr = expr or 'source_node_id != ""'
        rows = collection.query(
            expr=query_expr,
            output_fields=_FTS_RELATION_OUTPUT_FIELDS,
            limit=limit,
        )
        return rows or []
    except Exception as exc:
        logger.exception("FTS lexical pool query failed: %s", exc)
        return []


def _gap_fill_by_pool_nodes(
    pool_node_ids: list[str],
    relation_name: str,
    collection_name: str = "knowledge_university_relation",
    batch_size: int = 200,
) -> list[dict[str, Any]]:
    if not pool_node_ids or not relation_name:
        return []
    try:
        if not connect_milvus():
            return []
        if not utility.has_collection(collection_name):
            return []

        collection = Collection(name=collection_name)
        collection.load()

        def _quote(v: str) -> str:
            return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'

        all_rows: list[dict[str, Any]] = []
        deduped = list(dict.fromkeys(pool_node_ids))

        for i in range(0, len(deduped), batch_size):
            batch = deduped[i : i + batch_size]
            id_list = "[" + ",".join(_quote(nid) for nid in batch) + "]"
            # Match rows where pool node is EITHER src or dst
            expr = (
                f"relation_name == {_quote(relation_name)} and "
                f"(source_node_id in {id_list} or destination_node_id in {id_list})"
            )
            rows = collection.query(
                expr=expr,
                output_fields=_FTS_RELATION_OUTPUT_FIELDS,
                limit=min(len(batch) * 100, 16384),
            )
            if rows:
                all_rows.extend(rows)

        logger.info(
            "_gap_fill_by_pool_nodes: %d pool nodes, rel=%s → %d rows",
            len(deduped),
            relation_name,
            len(all_rows),
        )
        return all_rows

    except Exception as exc:
        logger.error("_gap_fill_by_pool_nodes error: %s", exc)
        return []


def fetch_relation_sibling_attributes(
    relation_pairs: list[dict[str, str]],
    collection_name: str = "knowledge_university_relation",
    batch_size: int = 200,
) -> list[dict[str, Any]]:
    """
    Fetch ALL attribute rows for given (source_node_id, destination_node_id, relation_name) tuples.

    When FTS finds e.g. estimated_score=17.0 for a Major↔AdmissionMethod relation,
    we also need cutoff_score=16.0 which didn't match the query text.
    This function fetches ALL sibling attribute rows for those same relation pairs.

    Args:
        relation_pairs: List of dicts with keys: source_node_id, destination_node_id, relation_name
        collection_name: Milvus collection name
        batch_size: Max pairs per query batch

    Returns:
        List of dicts with keys: source_node_id, destination_node_id, relation_name,
        attribute, attribute_value
    """
    if not relation_pairs:
        return []
    try:
        if not connect_milvus():
            return []
        if not utility.has_collection(collection_name):
            return []

        collection = Collection(name=collection_name)
        collection.load()

        # Deduplicate relation pairs
        unique_pairs = {}
        for p in relation_pairs:
            key = (p.get("source_node_id", ""), p.get("destination_node_id", ""), p.get("relation_name", ""))
            if all(key):
                unique_pairs[key] = p

        if not unique_pairs:
            return []

        def _quote(v: str) -> str:
            return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'

        all_rows: list[dict[str, Any]] = []
        pairs_list = list(unique_pairs.keys())

        for i in range(0, len(pairs_list), batch_size):
            batch = pairs_list[i : i + batch_size]
            # Build OR expression for each pair
            or_parts = []
            for src, dst, rel in batch:
                or_parts.append(
                    f"(source_node_id == {_quote(src)} and destination_node_id == {_quote(dst)} "
                    f"and relation_name == {_quote(rel)})"
                )
            expr = " or ".join(or_parts)
            rows = collection.query(
                expr=expr,
                output_fields=_FTS_RELATION_OUTPUT_FIELDS,
                limit=len(batch) * 50,  # assume max 50 attrs per relation
            )
            if rows:
                all_rows.extend(rows)

        logger.info(
            "fetch_relation_sibling_attributes: %d pairs → %d sibling rows",
            len(unique_pairs),
            len(all_rows),
        )
        return all_rows

    except Exception as exc:
        logger.error("fetch_relation_sibling_attributes error: %s", exc)
        return []


def _relation_candidate_key(entity: dict[str, Any]) -> str:
    return "||".join(
        [
            str(entity.get("source_node_id", "")),
            str(entity.get("destination_node_id", "")),
            str(entity.get("relation_name", "")),
            str(entity.get("attribute", "")),
            str(entity.get("attribute_value", "")),
        ]
    )


def _common_qa_candidate_key(entity: dict[str, Any]) -> str:
    entity_id = str(entity.get("id", "") or "").strip()
    if entity_id:
        return f"id::{entity_id}"

    return "||".join(
        [
            str(entity.get("question", "")),
            str(entity.get("answer", "")),
            str(entity.get("status", "")),
        ]
    )


@dataclass
class _CommonQACandidate:
    id: str
    question: str
    answer: str
    data_lv1: Any
    data_lv2: Any
    status: str
    created_at: int
    updated_at: int
    created_by: str
    updated_by: str
    semantic_raw: float = 0.0
    semantic_score: float = 0.0
    bm25_score: float = 0.0
    lexical_score: float = 0.0
    final_score: float = 0.0


def _normalize_common_qa_content(value: Any) -> Any:
    if value is None:
        return None

    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None

        lowered = stripped.casefold()
        if lowered in {"null", "none"}:
            return None

        if stripped[0] in {"{", "[", '"', "'"}:
            try:
                parsed = json.loads(stripped)
            except Exception:
                try:
                    parsed = ast.literal_eval(stripped)
                except Exception:
                    return stripped
            return _normalize_common_qa_content(parsed)

        return stripped

    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            normalized_item = _normalize_common_qa_content(item)
            if _common_qa_has_value(normalized_item):
                normalized[str(key)] = normalized_item
        return _unwrap_common_qa_nested_content(normalized or None)

    if isinstance(value, (list, tuple, set)):
        normalized_items = []
        for item in value:
            normalized_item = _normalize_common_qa_content(item)
            if _common_qa_has_value(normalized_item):
                normalized_items.append(normalized_item)
        return _unwrap_common_qa_nested_content(normalized_items or None)

    return value


def _common_qa_has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (dict, list, tuple, set)):
        return bool(value)
    return True


def _is_common_qa_response_envelope(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("source") == "knowledge_common_qa"
        and "answer" in value
        and isinstance(value.get("question"), str)
    )


def _is_common_qa_hit_payload(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if not isinstance(value.get("question"), str):
        return False
    if "status" not in value:
        return False
    return any(key in value for key in ("answer", "raw_answer", "final_score", "semantic_score", "score"))


def _extract_common_qa_hit_content(value: dict[str, Any]) -> Any:
    if _common_qa_has_value(value.get("data_lv1")) and _common_qa_has_value(value.get("data_lv2")):
        return {
            "data_lv1": value.get("data_lv1"),
            "data_lv2": value.get("data_lv2"),
        }
    if _common_qa_has_value(value.get("data_lv1")):
        return value.get("data_lv1")
    if _common_qa_has_value(value.get("data_lv2")):
        return value.get("data_lv2")
    if _common_qa_has_value(value.get("answer")):
        return value.get("answer")
    if _common_qa_has_value(value.get("raw_answer")):
        return value.get("raw_answer")
    return None


def _unwrap_common_qa_nested_content(value: Any) -> Any:
    if value is None:
        return None

    if isinstance(value, list):
        cleaned_items = []
        for item in value:
            unwrapped_item = _unwrap_common_qa_nested_content(item)
            if _common_qa_has_value(unwrapped_item):
                cleaned_items.append(unwrapped_item)
        if not cleaned_items:
            return None
        if len(cleaned_items) == 1:
            return cleaned_items[0]
        return cleaned_items

    if isinstance(value, dict):
        if _is_common_qa_response_envelope(value):
            return _unwrap_common_qa_nested_content(value.get("answer"))
        if _is_common_qa_hit_payload(value):
            return _unwrap_common_qa_nested_content(_extract_common_qa_hit_content(value))
        return value or None

    return value


def _stringify_common_qa_content(value: Any) -> str:
    normalized = _normalize_common_qa_content(value)
    if not _common_qa_has_value(normalized):
        return ""
    if isinstance(normalized, str):
        return normalized
    try:
        return json.dumps(normalized, ensure_ascii=False)
    except TypeError:
        return str(normalized)


def _build_common_qa_lexical_doc(entity: dict[str, Any]) -> str:
    question = str(entity.get("question", "") or "").strip()
    answer = str(entity.get("answer", "") or "").strip()
    data_lv1 = _normalize_common_qa_content(entity.get("data_lv1"))
    data_lv2 = _normalize_common_qa_content(entity.get("data_lv2"))

    parts = [question]
    if _common_qa_has_value(data_lv1) or _common_qa_has_value(data_lv2):
        if _common_qa_has_value(data_lv1):
            parts.append(_stringify_common_qa_content(data_lv1))
        if _common_qa_has_value(data_lv2):
            parts.append(_stringify_common_qa_content(data_lv2))
    elif answer:
        parts.append(answer)

    return " ".join(part for part in parts if part).strip()


def _resolve_common_qa_answer_payload(
    raw_answer: Any,
    raw_data_lv1: Any,
    raw_data_lv2: Any,
) -> dict[str, Any]:
    normalized_data_lv1 = _normalize_common_qa_content(raw_data_lv1)
    normalized_data_lv2 = _normalize_common_qa_content(raw_data_lv2)
    normalized_answer = str(raw_answer or "")

    payload: dict[str, Any] = {
        "raw_answer": normalized_answer,
        "data_lv1": normalized_data_lv1,
        "data_lv2": normalized_data_lv2,
    }

    if _common_qa_has_value(normalized_data_lv1) and _common_qa_has_value(normalized_data_lv2):
        payload["answer"] = {
            "data_lv1": normalized_data_lv1,
            "data_lv2": normalized_data_lv2,
        }
        payload["answer_source"] = "data_lv1+data_lv2"
        return payload

    if _common_qa_has_value(normalized_data_lv1):
        payload["answer"] = {"data_lv1": normalized_data_lv1}
        payload["answer_source"] = "data_lv1"
        return payload

    if _common_qa_has_value(normalized_data_lv2):
        payload["answer"] = {"data_lv2": normalized_data_lv2}
        payload["answer_source"] = "data_lv2"
        return payload

    payload["answer"] = normalized_answer
    payload["answer_source"] = "answer"
    return payload


def _common_qa_answer_signature(value: Any) -> str:
    normalized = _normalize_common_qa_content(value)
    if not _common_qa_has_value(normalized):
        return ""

    if isinstance(normalized, str):
        return " ".join(_fts_tokenize(normalized))

    if isinstance(normalized, dict):
        if set(normalized.keys()).issubset({"data_lv1", "data_lv2"}):
            parts = [_common_qa_answer_signature(normalized.get(key)) for key in ("data_lv1", "data_lv2")]
            return "||".join(part for part in parts if part)

        parts = []
        for key in sorted(normalized.keys()):
            item_signature = _common_qa_answer_signature(normalized.get(key))
            if item_signature:
                parts.append(f"{key}:{item_signature}")
        return "||".join(parts)

    if isinstance(normalized, list):
        parts = [_common_qa_answer_signature(item) for item in normalized]
        return "||".join(part for part in parts if part)

    return " ".join(_fts_tokenize(str(normalized)))


def _fts_search_common_qa_semantic(
    collection: Collection,
    query_embedding: list[list[float]],
    limit: int,
    expr: str | None = None,
) -> list[Any]:
    """Vector search for common QA entries using the single `embedding` field."""
    search_params = {"metric_type": "COSINE", "params": {"ef": 200}}
    try:
        results = collection.search(
            data=query_embedding,
            anns_field="embedding",
            param=search_params,
            limit=limit,
            expr=expr,
            output_fields=[
                "id",
                "question",
                "answer",
                "data_lv1",
                "data_lv2",
                "status",
                "created_at",
                "updated_at",
                "created_by",
                "updated_by",
            ],
        )
    except Exception as exc:
        logger.error("FTS common QA semantic search failed: %s", exc)
        return []
    return results[0] if results else []


def _fts_fetch_common_qa_lexical_pool(
    collection: Collection,
    limit: int,
    expr: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch common QA rows for lexical/BM25 scoring."""
    try:
        rows = collection.query(
            expr=expr if expr else "",
            output_fields=[
                "id",
                "question",
                "answer",
                "data_lv1",
                "data_lv2",
                "status",
                "created_at",
                "updated_at",
                "created_by",
                "updated_by",
            ],
            limit=limit,
        )
        return rows if rows else []
    except Exception as exc:
        logger.error("FTS common QA lexical pool query failed: %s", exc)
        return []


def full_text_search_common_qa(
    query: str,
    collection: Collection,
    top_k: int = 3,
    candidate_k: int = 20,
    lexical_pool_limit: int = 3000,
    lexical_top_k: int = 300,
    similarity_threshold: float = 0.35,
    semantic_weight: float = 0.7,
    bm25_weight: float = 0.2,
    lexical_weight: float = 0.1,
    min_lexical_signal: float = 0.02,
    semantic_rescue_threshold: float = 0.7,
    semantic_rescue_requires_value: bool = False,
    require_lexical_signal: bool = True,
    min_final_score: float = 0.0,
    expr: str | None = None,
) -> list[dict[str, Any]]:
    """
    Full hybrid retrieval for common QA using a single semantic field plus
    lexical/BM25 fusion.
    """
    clean_query = (query or "").strip()
    if not clean_query:
        return []

    total = semantic_weight + bm25_weight + lexical_weight
    if total <= 0:
        semantic_weight, bm25_weight, lexical_weight = 1.0, 0.0, 0.0
    elif not math.isclose(total, 1.0):
        semantic_weight /= total
        bm25_weight /= total
        lexical_weight /= total

    def _build_candidate(entity: dict[str, Any]) -> _CommonQACandidate:
        return _CommonQACandidate(
            id=str(entity.get("id", "") or ""),
            question=str(entity.get("question", "")),
            answer=str(entity.get("answer", "")),
            data_lv1=entity.get("data_lv1"),
            data_lv2=entity.get("data_lv2"),
            status=str(entity.get("status", "")),
            created_at=int(entity.get("created_at", 0) or 0),
            updated_at=int(entity.get("updated_at", 0) or 0),
            created_by=str(entity.get("created_by", "") or ""),
            updated_by=str(entity.get("updated_by", "") or ""),
        )

    lexical_rows = _fts_fetch_common_qa_lexical_pool(collection, lexical_pool_limit, expr)
    logger.info("FTS common QA lexical pool rows fetched: %d", len(lexical_rows))

    lexical_candidate_map: dict[str, dict[str, Any]] = {}
    if lexical_rows:
        lexical_docs = [_build_common_qa_lexical_doc(row) for row in lexical_rows]
        pool_bm25 = _fts_bm25_scores(clean_query, lexical_docs)
        pool_overlap = [_fts_lexical_overlap(clean_query, doc) for doc in lexical_docs]

        ranked_idx = sorted(
            range(len(lexical_rows)),
            key=lambda i: max(pool_bm25[i], pool_overlap[i]),
            reverse=True,
        )

        added = 0
        for idx in ranked_idx:
            if added >= lexical_top_k:
                break
            signal = max(pool_bm25[idx], pool_overlap[idx])
            if signal <= 0.0:
                break

            row = lexical_rows[idx]
            key = _common_qa_candidate_key(row)
            if key in lexical_candidate_map:
                continue
            lexical_candidate_map[key] = {
                "entity": row,
                "bm25_raw": pool_bm25[idx],
                "lexical_raw": pool_overlap[idx],
            }
            added += 1

        logger.info("FTS common QA lexical candidates added: %d", added)

    candidates = []
    for signal in lexical_candidate_map.values():
        candidate = _build_candidate(signal.get("entity", {}))
        candidate.bm25_score = _fts_bm25_to_confidence(signal.get("bm25_raw", 0.0))
        candidate.lexical_score = _fts_clip_unit_interval(signal.get("lexical_raw", 0.0))
        candidates.append(candidate)

    query_embedding = get_embedding([clean_query])
    semantic_score_map: dict[str, float] = {}
    if query_embedding and query_embedding[0]:
        semantic_hits = _fts_search_common_qa_semantic(collection, query_embedding, candidate_k, expr)
        # logger.info(f"FTS common QA semantic hits:{semantic_hits}" )
        # semantic_debug_rows = []
        for hit in semantic_hits:
            raw_score = float(hit.score)
            if raw_score < similarity_threshold:
                continue
            entity = {
                "id": hit.entity.get("id", ""),
                "question": hit.entity.get("question", ""),
                "answer": hit.entity.get("answer", ""),
                "data_lv1": hit.entity.get("data_lv1"),
                "data_lv2": hit.entity.get("data_lv2"),
                "status": hit.entity.get("status", ""),
                "created_at": hit.entity.get("created_at", 0),
                "updated_at": hit.entity.get("updated_at", 0),
                "created_by": hit.entity.get("created_by", ""),
                "updated_by": hit.entity.get("updated_by", ""),
            }
            key = _common_qa_candidate_key(entity)
            semantic_score_map[key] = max(semantic_score_map.get(key, 0.0), raw_score)

            if key not in lexical_candidate_map:
                candidate = _build_candidate(entity)
                candidates.append(candidate)
    else:
        logger.error("FTS common QA: failed to generate query embedding")

    if not candidates:
        logger.info("FTS common QA: no candidates")
        return []

    for candidate in candidates:
        key = _common_qa_candidate_key(
            {
                "id": candidate.id,
                "question": candidate.question,
                "answer": candidate.answer,
                "status": candidate.status,
            }
        )
        candidate.semantic_raw = semantic_score_map.get(key, 0.0)
        candidate.semantic_score = _fts_clip_unit_interval(candidate.semantic_raw)

    scored_rows = [
        (
            candidate,
            candidate.semantic_score,
            candidate.bm25_score,
            candidate.lexical_score,
            candidate.semantic_raw,
        )
        for candidate in candidates
    ]

    lexical_signal = [max(row[2], row[3]) for row in scored_rows]
    lexical_positive_count = sum(1 for signal in lexical_signal if signal >= min_lexical_signal)

    if require_lexical_signal:
        filtered: list[tuple[_CommonQACandidate, float, float, float, float]] = []
        if lexical_positive_count > 0:
            logger.info(
                "FTS common QA lexical signal detected (%d). Applying gate >= %.3f",
                lexical_positive_count,
                min_lexical_signal,
            )
            for row in scored_rows:
                if max(row[2], row[3]) >= min_lexical_signal:
                    filtered.append(row)
        else:
            logger.info(
                "FTS common QA no lexical signal. Semantic rescue threshold >= %.3f",
                semantic_rescue_threshold,
            )
            for row in scored_rows:
                candidate = row[0]
                if semantic_rescue_requires_value and candidate.lexical_score < min_lexical_signal:
                    continue
                if row[4] >= semantic_rescue_threshold:
                    filtered.append(row)

        if not filtered:
            logger.warning(
                "FTS common QA all candidates failed gate (min_lexical=%.3f, rescue=%.3f).",
                min_lexical_signal,
                semantic_rescue_threshold,
            )
            return []

        scored_rows = filtered
        logger.info("FTS common QA candidates after gate: %d", len(scored_rows))

    semantic_raw_rank = _fts_normalize_scores([row[4] for row in scored_rows])
    bm25_rank = _fts_normalize_scores([row[2] for row in scored_rows])
    lexical_rank = _fts_normalize_scores([row[3] for row in scored_rows])

    ranked_candidates: list[tuple[_CommonQACandidate, float]] = []
    for idx, (candidate, semantic_score, bm25_score, lexical_score, _) in enumerate(scored_rows):
        candidate.semantic_score = semantic_score
        candidate.bm25_score = bm25_score
        candidate.lexical_score = lexical_score
        semantic_component = semantic_weight * candidate.semantic_score
        bm25_component = bm25_weight * candidate.bm25_score
        lexical_component = lexical_weight * candidate.lexical_score
        candidate.final_score = (
            semantic_component
            + bm25_component
            + lexical_component
        )
        tie_break_rank = (
            semantic_weight * semantic_raw_rank[idx]
            + bm25_weight * bm25_rank[idx]
            + lexical_weight * lexical_rank[idx]
        )
        ranked_candidates.append((candidate, tie_break_rank))

    ranked_candidates.sort(
        key=lambda item: (
            item[0].final_score,
            item[1],
            item[0].semantic_score,
            item[0].lexical_score,
            item[0].bm25_score,
        ),
        reverse=True,
    )

    ranked_output = [item[0] for item in ranked_candidates]
    if min_final_score > 0.0:
        before = len(ranked_output)
        ranked_output = [item for item in ranked_output if item.final_score >= min_final_score]
        logger.info(
            "FTS common QA min_final_score=%.3f: %d -> %d results",
            min_final_score,
            before,
            len(ranked_output),
        )

    output: list[dict[str, Any]] = []
    seen_signatures: set[str] = set()
    for candidate in ranked_output:
        resolved_answer = _resolve_common_qa_answer_payload(candidate.answer, candidate.data_lv1, candidate.data_lv2)
        question_signature = " ".join(_fts_tokenize(candidate.question))
        answer_signature = _common_qa_answer_signature(resolved_answer["answer"])
        dedupe_signature = f"{question_signature}||{answer_signature}"

        if answer_signature and dedupe_signature in seen_signatures:
            logger.info(
                "FTS common QA skipping duplicate candidate for question='%s'",
                candidate.question[:120],
            )
            continue

        if answer_signature:
            seen_signatures.add(dedupe_signature)

        output.append(
            {
                "question": candidate.question,
                "answer": resolved_answer["answer"],
                "raw_answer": resolved_answer["raw_answer"],
                "data_lv1": resolved_answer["data_lv1"],
                "data_lv2": resolved_answer["data_lv2"],
                "answer_source": resolved_answer["answer_source"],
                "status": candidate.status,
                "created_at": candidate.created_at,
                "updated_at": candidate.updated_at,
                "created_by": candidate.created_by,
                "updated_by": candidate.updated_by,
                "semantic_raw": round(candidate.semantic_raw, 6),
                "semantic_score": round(candidate.semantic_score, 6),
                "bm25_score": round(candidate.bm25_score, 6),
                "lexical_score": round(candidate.lexical_score, 6),
                "final_score": round(candidate.final_score, 6),
            }
        )
        if len(output) >= top_k:
            break

    for idx, item in enumerate(output[:5], 1):
        logger.info(
            "FTS common QA #%d final=%.4f semantic=%.4f bm25=%.4f lexical=%.4f q=%s",
            idx,
            item["final_score"],
            item["semantic_score"],
            item["bm25_score"],
            item["lexical_score"],
            item["question"][:80],
        )

    return output


def full_text_search_relations(
    query: str,
    collection: Collection,
    top_k: int = 30,
    candidate_k: int = 80,
    lexical_pool_limit: int = 5000,
    lexical_top_k: int = 300,
    similarity_threshold: float = 0.35,
    semantic_weight: float = 0.7,
    bm25_weight: float = 0.2,
    lexical_weight: float = 0.1,
    min_lexical_signal: float = 0.02,
    semantic_rescue_threshold: float = 0.6,
    semantic_rescue_requires_value: bool = True,
    require_lexical_signal: bool = True,
    expr: str | None = None,
) -> list[dict[str, Any]]:
    """
    Hybrid retrieval over a pre-loaded Milvus relation collection.

    Combines:
    - Semantic search on `embedding_attribute` AND `embedding_content` (parallel)
    - Lexical retrieval from collection rows (BM25 + lexical overlap)
    - Weighted score fusion with absolute confidence for thresholding
      and relative min-max scores for tie-break ranking
    - Adaptive lexical gate / semantic rescue

    Args:
        query:           Single search query string.
        collection:      Already-loaded Milvus Collection object.
        expr:            Optional Milvus filter expression (e.g. node_id filter).
    Returns:
        List of result dicts with keys: source_node_id, destination_node_id,
        relation_name, attribute, attribute_value, semantic_score, bm25_score,
        lexical_score, final_score, semantic_attr_raw, semantic_value_raw.
    """
    clean_query = (query or "").strip()
    if not clean_query:
        return []

    # Normalise weights
    total = semantic_weight + bm25_weight + lexical_weight
    if total <= 0:
        semantic_weight, bm25_weight, lexical_weight = 1.0, 0.0, 0.0
    elif not math.isclose(total, 1.0):
        semantic_weight /= total
        bm25_weight /= total
        lexical_weight /= total

    query_embedding = get_embedding([clean_query])
    if not query_embedding or not query_embedding[0]:
        logger.error("FTS: failed to generate embedding for query '%s'", clean_query)
        return []

    hits_attr = _fts_search_one_field(collection, query_embedding, "embedding_attribute", candidate_k, expr)
    hits_value = _fts_search_one_field(collection, query_embedding, "embedding_content", candidate_k, expr)
    logger.info("FTS raw semantic hits: attr=%d value=%d", len(hits_attr), len(hits_value))

    candidate_map: dict[str, _RelationCandidate] = {}

    def _add_hit(hit: Any, field: str) -> None:
        score = float(hit.score)
        if score < similarity_threshold:
            return
        entity = {f: hit.entity.get(f, "") for f in _FTS_RELATION_OUTPUT_FIELDS}
        key = _relation_candidate_key(entity)
        if key not in candidate_map:
            candidate_map[key] = _RelationCandidate(
                source_node_id=str(entity.get("source_node_id", "")),
                destination_node_id=str(entity.get("destination_node_id", "")),
                relation_name=str(entity.get("relation_name", "")),
                attribute=str(entity.get("attribute", "")),
                attribute_value=str(entity.get("attribute_value", "")),
            )
        if field == "embedding_attribute":
            candidate_map[key].semantic_attr_raw = max(candidate_map[key].semantic_attr_raw, score)
        else:
            candidate_map[key].semantic_value_raw = max(candidate_map[key].semantic_value_raw, score)

    for hit in hits_attr:
        _add_hit(hit, "embedding_attribute")
    for hit in hits_value:
        _add_hit(hit, "embedding_content")

    lexical_rows = _fts_fetch_lexical_pool(collection, lexical_pool_limit, expr)
    logger.info("FTS lexical pool rows fetched: %d", len(lexical_rows))

    if lexical_rows:
        lexical_docs = [f"{r.get('attribute_value', '')}".strip() for r in lexical_rows]
        pool_bm25 = _fts_bm25_scores(clean_query, lexical_docs)
        pool_overlap = [_fts_lexical_overlap(clean_query, doc) for doc in lexical_docs]

        ranked_idx = sorted(
            range(len(lexical_rows)),
            key=lambda i: max(pool_bm25[i], pool_overlap[i]),
            reverse=True,
        )
        added = 0
        for idx in ranked_idx:
            if added >= lexical_top_k:
                break
            signal = max(pool_bm25[idx], pool_overlap[idx])
            if signal <= 0.0:
                break
            row = lexical_rows[idx]
            key = _relation_candidate_key(row)
            if key not in candidate_map:
                candidate_map[key] = _RelationCandidate(
                    source_node_id=str(row.get("source_node_id", "")),
                    destination_node_id=str(row.get("destination_node_id", "")),
                    relation_name=str(row.get("relation_name", "")),
                    attribute=str(row.get("attribute", "")),
                    attribute_value=str(row.get("attribute_value", "")),
                )
                added += 1
        logger.info("FTS candidates added from lexical retrieval: %d", added)

    candidates = list(candidate_map.values())
    if not candidates:
        logger.info("FTS: no candidates above threshold %.3f", similarity_threshold)
        return []

    logger.info("FTS candidates before gate: %d", len(candidates))

    corpus = [f"{c.attribute_value}".strip() for c in candidates]
    bm25_raw = _fts_bm25_scores(clean_query, corpus)
    lexical_raw = [_fts_lexical_overlap(clean_query, doc) for doc in corpus]
    semantic_raw = [max(c.semantic_attr_raw, c.semantic_value_raw) for c in candidates]

    # Absolute scores are used for confidence thresholding / filtering.
    # Relative min-max scores are kept only as a tie-break when many candidates
    # have similar absolute confidence within the same query.
    bm25_conf = [_fts_bm25_to_confidence(score) for score in bm25_raw]
    lexical_conf = [_fts_clip_unit_interval(score) for score in lexical_raw]
    semantic_conf = [_fts_clip_unit_interval(score) for score in semantic_raw]

    bm25_rank = _fts_normalize_scores(bm25_raw)
    lexical_rank = _fts_normalize_scores(lexical_raw)
    semantic_rank = _fts_normalize_scores(semantic_raw)

    scored_rows = [
        (
            candidate,
            semantic_conf[idx],
            bm25_conf[idx],
            lexical_conf[idx],
            semantic_rank[idx],
            bm25_rank[idx],
            lexical_rank[idx],
            semantic_raw[idx],
        )
        for idx, candidate in enumerate(candidates)
    ]

    lexical_signal = [max(row[2], row[3]) for row in scored_rows]
    lexical_positive_count = sum(1 for s in lexical_signal if s >= min_lexical_signal)

    if require_lexical_signal:
        filtered: list[tuple] = []
        if lexical_positive_count > 0:
            logger.info(
                "FTS lexical signal detected (%d). Applying gate >= %.3f",
                lexical_positive_count,
                min_lexical_signal,
            )
            for row in scored_rows:
                if max(row[2], row[3]) >= min_lexical_signal:
                    filtered.append(row)
        else:
            logger.info(
                "FTS no lexical signal. Semantic rescue threshold >= %.3f",
                semantic_rescue_threshold,
            )
            for row in scored_rows:
                candidate = row[0]
                has_value_support = candidate.semantic_value_raw >= similarity_threshold
                if semantic_rescue_requires_value and not has_value_support:
                    continue
                if row[7] >= semantic_rescue_threshold:
                    filtered.append(row)

        if not filtered:
            logger.warning(
                "FTS all candidates failed gate (min_lexical=%.3f, rescue=%.3f).",
                min_lexical_signal,
                semantic_rescue_threshold,
            )
            return []

        scored_rows = filtered
        logger.info("FTS candidates after gate: %d", len(scored_rows))

    ranked_candidates: list[tuple[_RelationCandidate, float]] = []
    for (
        c,
        semantic_score,
        bm25_score,
        lexical_score,
        semantic_rank_score,
        bm25_rank_score,
        lexical_rank_score,
        _,
    ) in scored_rows:
        c.bm25_score = bm25_score
        c.lexical_score = lexical_score
        c.semantic_score = semantic_score
        c.final_score = (
            semantic_weight * c.semantic_score + bm25_weight * c.bm25_score + lexical_weight * c.lexical_score
        )
        rank_score = (
            semantic_weight * semantic_rank_score + bm25_weight * bm25_rank_score + lexical_weight * lexical_rank_score
        )
        ranked_candidates.append((c, rank_score))

    ranked_candidates.sort(
        key=lambda item: (
            item[0].final_score,
            item[1],
            item[0].semantic_score,
            item[0].lexical_score,
            item[0].bm25_score,
        ),
        reverse=True,
    )
    top_candidates = [item[0] for item in ranked_candidates[:top_k]]

    output: list[dict[str, Any]] = []
    for c in top_candidates:
        output.append(
            {
                "source_node_id": c.source_node_id,
                "destination_node_id": c.destination_node_id,
                "relation_name": c.relation_name,
                "attribute": c.attribute,
                "attribute_value": c.attribute_value,
                "semantic_attr_raw": round(c.semantic_attr_raw, 6),
                "semantic_value_raw": round(c.semantic_value_raw, 6),
                "semantic_score": round(c.semantic_score, 6),
                "bm25_score": round(c.bm25_score, 6),
                "lexical_score": round(c.lexical_score, 6),
                "final_score": round(c.final_score, 6),
            }
        )

    # logger.info("FTS fusion candidates: %d", len(ranked_candidates))
    for idx, item in enumerate(output[:5], 1):
        logger.info(
            "FTS #%d final=%.4f semantic=%.4f bm25=%.4f lexical=%.4f rel=%s attr=%s",
            idx,
            item["final_score"],
            item["semantic_score"],
            item["bm25_score"],
            item["lexical_score"],
            item["relation_name"],
            item["attribute"],
        )
    return output


def search_relation_full_text(
    node_ids: list[str],
    queries: list[str],
    top_k: int = 30,
    similarity_threshold: float = 0.35,
    min_final_score: float = 0.3,
    collection_name: str = "knowledge_university_relation",
    node_ids_batch_size: int = 200,
    expr: str | None = None,
) -> list[dict[str, Any]]:
    """
    Drop-in replacement for `search_relation_attributes_hybrid` using full hybrid
    retrieval (semantic + BM25 + lexical overlap) instead of sequential fallback.

    Accepts the same signature as `search_relation_attributes_hybrid` for seamless
    YAML query-plan integration.

    For each (query × node_id_batch) pair:
    - Builds a Milvus filter expression using `IN [...]` on node-id batches
      (source OR destination) so lexical/BM25 pools are scoped to relevant
      rows with fewer Milvus round-trips than per-node querying.
    - Calls `full_text_search_relations` with the scoped expr.
    - Merges and deduplicates all results, keeping the best score per candidate.

    Args:
        node_ids:  Context node IDs already resolved (e.g. from context_found_list).
        queries:   List of search query strings (e.g. keywords / attribute names).
        top_k:     Maximum number of results to return per (query × node) call;
                   overall results are further sorted and capped by top_k globally.
        similarity_threshold: Minimum raw cosine similarity to admit a vector hit.
        min_final_score: Minimum fused final_score (0–1) to include in result.
                   Applied AFTER dedup & sort. Helps cut low-confidence results.
                   Set to 0.0 to disable.
        collection_name: Milvus collection (default: knowledge_university_relation).
        node_ids_batch_size: Batch size for `IN [...]` filter on node_ids.
                   Smaller size reduces expression length; larger size reduces
                   number of round-trips.
        expr:      Additional Milvus filter expression (ANDed with node filter).

    Returns:
        List of dicts with keys: source_node_id, destination_node_id,
        relation_name, attribute, attribute_value, semantic_score,
        bm25_score, lexical_score, final_score, semantic_attr_raw,
        semantic_value_raw.
    """
    try:
        if not queries:
            return []

        if not connect_milvus():
            return []

        if not utility.has_collection(collection_name):
            logger.info("FTS: collection '%s' does not exist", collection_name)
            return []

        collection = Collection(name=collection_name)
        collection.load()

        best_map: dict[str, dict[str, Any]] = {}

        def _merge_result(r: dict[str, Any]) -> None:
            key = _relation_candidate_key(r)
            if key not in best_map or r["final_score"] > best_map[key]["final_score"]:
                best_map[key] = r

        jobs: list[tuple[str, str | None, str | None]] = []
        if node_ids:
            deduped_node_ids = list(dict.fromkeys(node_ids))
            batch_size = max(1, int(node_ids_batch_size))

            def _quote_id(value: str) -> str:
                return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'

            node_id_batches = [
                deduped_node_ids[i : i + batch_size] for i in range(0, len(deduped_node_ids), batch_size)
            ]

            for batch_idx, batch_ids in enumerate(node_id_batches, start=1):
                id_list_expr = "[" + ",".join(_quote_id(nid) for nid in batch_ids) + "]"
                nid_expr = f"(source_node_id in {id_list_expr}) or (destination_node_id in {id_list_expr})"
                combined_expr = f"({expr}) and ({nid_expr})" if expr else nid_expr
                batch_tag = f"batch-{batch_idx}/{len(node_id_batches)}({len(batch_ids)} ids)"
                for query in queries:
                    jobs.append((query, combined_expr, batch_tag))
        else:
            # No node_ids: search globally (fallback)
            for query in queries:
                jobs.append((query, expr, None))

        def _run_job(job: tuple[str, str | None, str | None]) -> list[dict[str, Any]]:
            query, job_expr, scope_tag = job
            if scope_tag is not None:
                logger.info("FTS query='%s' scope='%s'", query, scope_tag)
            else:
                logger.info("FTS query='%s' (global, no node filter)", query)
            return full_text_search_relations(
                query=query,
                collection=collection,
                top_k=top_k,
                similarity_threshold=similarity_threshold,
                expr=job_expr,
            )

        for job in jobs:
            for result in _run_job(job):
                _merge_result(result)

        all_results = sorted(best_map.values(), key=lambda x: x["final_score"], reverse=True)

        # Filter by min_final_score
        if min_final_score > 0.0:
            before = len(all_results)
            all_results = [r for r in all_results if r["final_score"] >= min_final_score]
            logger.info(
                "FTS min_final_score=%.3f: %d → %d results",
                min_final_score,
                before,
                len(all_results),
            )

        logger.info(
            "search_relation_full_text: %d unique results from %d jobs (%d queries, %d node_ids)",
            len(all_results),
            len(jobs),
            len(queries),
            len(node_ids) if node_ids else 0,
        )
        return all_results

    except Exception as exc:
        logger.error("Error in search_relation_full_text: %s", exc)
        traceback.print_exc()
        return []


# Export main functions


__all__ = [
    "get_embedding",
    "connect_milvus",
    "search_similar_documents",
    "search_chunks_by_query",
    "search_entity_by_name",
    "get_nodes_by_type",
    "semantic_resolve_attributes",
    "search_school_by_name_like",
    "search_school_hybrid",
    "get_node_name_by_id",
    "ensure_crawled_data_collection",
    "search_crawled_data",
    "search_common_qa",
    "insert_crawled_data",
    "generate_and_save_crawled_data",
    "search_relation_full_text",
    "full_text_search_common_qa",
    "full_text_search_relations",
    "fetch_relation_sibling_attributes",
]


def generate_and_save_crawled_data(
    question: str,
    collection_name: str = "knowledge_university_crawled_data",
) -> list[dict[str, Any]]:
    """
    Fallback: Generate answer using LLM (Chat Completion) and save to Milvus.

    Args:
        question: List of questions (usually just one original_query)
        collection_name: Name of the FAQ collection

    Returns:
        List of dicts containing the generated content, similar to search_crawled_data output.
    """
    try:
        if not question:
            return []

        # question = question  # Take the first query
        # logger.info(f"Triggering chat completion fallback for: {question}")

        # 1. Generate answer using LLM
        generated_content = run_parallel_web_search_sync(question, is_school=True)

        if not generated_content:
            logger.warning("LLM generated empty content")
            return []

        data_crawled = generated_content.get("summary", "")
        source_types = generated_content.get("source_types", [])
        source_urls = generated_content.get("source_urls", [])

        insert_success = insert_crawled_data(
            question=question,
            content=data_crawled,
            source_type=source_types,
            source_url=source_urls,
            supporting_node_type=[],
        )

        if insert_success:
            logger.info("Successfully saved generated content to Milvus")
        else:
            logger.error("Failed to save generated content to Milvus")

        # 3. Return formatted result
        return [
            {
                "content": generated_content,
                "score": 1.0,  # High confidence since it's direct generation
                "source_url": [],
                "status": "auto_generated",
            }
        ]

    except Exception as e:
        logger.error(f"Error in generate_and_save_crawled_data: {e}")
        traceback.print_exc()
        return []


def search_relation_attribute(
    attribute: str,
    top_k: int = 5,
    threshold: float = 0.6,
    node_id: str | None = None,
) -> list[dict[str, Any]]:
    """
    Search for relation attributes in 'knowledge_university_relation' collection.

    Args:
        node_id: The source_node_id to filter by/check against.
        attribute: Name of the attribute (e.g. "fee_information") or keyword to title match.
        top_k: Number of results to fetch.
        threshold: Similarity threshold.

    Returns:
        List of matching results (dicts) that match the source_node_id.
    """
    try:
        # 1. Construct text for embedding
        # User requested to only receive 'attribute'. We embed the attribute text itself.
        embed_text = attribute

        logger.info(f"Searching relation attribute with text: '{embed_text}' for node_id: {node_id}")

        # 2. Get embedding
        query_embedding = get_embedding([embed_text])
        if not query_embedding:
            return []

        # 3. Search in collection
        expr = f'destination_node_id == "{node_id}"' if node_id else None

        results = search_similar_documents(
            collection_name="knowledge_university_relation",
            query_vector=query_embedding,
            top_k=top_k,
            threshold=threshold,
            expr=expr,
            anns_field="embedding",
            output_fields=[
                "id",
                "source_node_id",
                "destination_node_id",
                "relation_name",
                "attribute",
                "attribute_value",
            ],
        )

        if not results:
            logger.info(f"No relation attributes found for node_id {node_id} with threshold {threshold}")
            return []

        return results

    except Exception as e:
        logger.error(f"Error searching relation attribute: {e}")
        traceback.print_exc()
        return []


def search_relation_attributes_by_keywords(
    node_id: str, keywords: list[str], top_k: int = 5, threshold: float = 0.6
) -> list[dict[str, Any]]:
    """
    Wrapper to search relation attributes for multiple keywords.
    """
    all_results = []
    if not keywords:
        return []

    for keyword in keywords:
        results = search_relation_attribute(node_id=node_id, attribute=keyword, top_k=top_k, threshold=threshold)
        if results:
            all_results.extend(results)

    return all_results
