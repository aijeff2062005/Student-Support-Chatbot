#!/usr/bin/env python3
"""
Hybrid full-text + semantic search utility for Milvus.

Usage:
  python tests/full_text_search.py "hoc phi nganh cntt"
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import re
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Allow running this file directly from project root.
sys.path.insert(0, str(Path(__file__).parent.parent))

from pymilvus import Collection, utility

from configs.config_service import get_settings
from dbs.milvus_helper import connect_milvus, get_embedding

logger = logging.getLogger("tests.full_text_search")
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


ATTR_VECTOR_FIELD = "embedding_attribute"
VALUE_VECTOR_FIELD = "embedding_content"
DEFAULT_COLLECTION = "knowledge_university_relation"
SEARCH_OUTPUT_FIELDS = [
    "source_node_id",
    "destination_node_id",
    "relation_name",
    "attribute",
    "attribute_value",
]


@dataclass
class Candidate:
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


_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def _fold_text(text: str) -> str:
    if not text:
        return ""
    text = text.lower().replace("đ", "d")
    return "".join(ch for ch in unicodedata.normalize("NFD", text) if unicodedata.category(ch) != "Mn")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(_fold_text(text))


def _lexical_overlap(query: str, document: str) -> float:
    query_tokens = set(_tokenize(query))
    if not query_tokens:
        return 0.0
    doc_tokens = set(_tokenize(document))
    if not doc_tokens:
        return 0.0
    return len(query_tokens & doc_tokens) / len(query_tokens)


def _normalize_scores(scores: list[float]) -> list[float]:
    if not scores:
        return []
    mn, mx = min(scores), max(scores)
    if math.isclose(mn, mx):
        if math.isclose(mx, 0.0):
            return [0.0] * len(scores)
        return [0.5] * len(scores)
    return [(s - mn) / (mx - mn) for s in scores]


def _bm25_scores(query: str, documents: list[str], k1: float = 1.5, b: float = 0.75) -> list[float]:
    query_tokens = _tokenize(query)
    if not query_tokens or not documents:
        return [0.0] * len(documents)

    tokenized_docs = [_tokenize(doc) for doc in documents]
    total_docs = len(tokenized_docs)
    avg_doc_len = sum(len(doc) for doc in tokenized_docs) / max(total_docs, 1)
    avg_doc_len = max(avg_doc_len, 1.0)

    doc_freq: dict[str, int] = {}
    for doc in tokenized_docs:
        for token in set(doc):
            doc_freq[token] = doc_freq.get(token, 0) + 1

    idf: dict[str, float] = {}
    for token in set(query_tokens):
        df = doc_freq.get(token, 0)
        idf[token] = math.log(1.0 + (total_docs - df + 0.5) / (df + 0.5))

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


def _search_one_field(
    collection: Collection,
    query_embedding: list[list[float]],
    anns_field: str,
    limit: int,
    expr: str | None = None,
) -> list[Any]:
    search_params = {"metric_type": "COSINE", "params": {"ef": 200}}
    try:
        result = collection.search(
            data=query_embedding,
            anns_field=anns_field,
            param=search_params,
            limit=limit,
            expr=expr,
            output_fields=SEARCH_OUTPUT_FIELDS,
        )
        return result[0] if result else []
    except Exception as exc:
        logger.exception("Search failed on field '%s': %s", anns_field, exc)
        return []


def _fetch_lexical_pool(
    collection: Collection,
    limit: int,
    expr: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch rows for lexical scoring without relying on vector candidates."""
    try:
        query_expr = expr or 'source_node_id != ""'
        rows = collection.query(
            expr=query_expr,
            output_fields=SEARCH_OUTPUT_FIELDS,
            limit=limit,
        )
        return rows or []
    except Exception as exc:
        logger.exception("Lexical pool query failed: %s", exc)
        return []


def full_text_search(
    query: str,
    collection_name: str = DEFAULT_COLLECTION,
    top_k: int = 10,
    candidate_k: int = 80,
    lexical_pool_limit: int = 10000,
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
    Hybrid retrieval over Milvus relation collection.

    - Semantic search on `embedding_attribute` and `embedding_content`
    - Lexical retrieval directly from collection rows (independent from vector candidates)
    - BM25 + lexical overlap over candidate text (`relation_name + attribute + attribute_value`)
    - Adaptive lexical gate; semantic rescue only when lexical has no signal
    - Weighted score fusion to improve recall/precision
    """
    clean_query = (query or "").strip()
    if not clean_query:
        logger.warning("Empty query")
        return []

    total = semantic_weight + bm25_weight + lexical_weight
    if total <= 0:
        logger.warning("Invalid fusion weights; fallback to semantic-only")
        semantic_weight, bm25_weight, lexical_weight = 1.0, 0.0, 0.0
    elif not math.isclose(total, 1.0):
        semantic_weight = semantic_weight / total
        bm25_weight = bm25_weight / total
        lexical_weight = lexical_weight / total

    settings = get_settings()
    logger.info(
        "Milvus target host=%s port=%s db=%s collection=%s",
        settings.milvus_host,
        settings.milvus_port,
        settings.milvus_database or "default",
        collection_name,
    )
    logger.info("Query: %s", clean_query)

    if not connect_milvus(db_name=settings.milvus_database):
        logger.error("Cannot connect to Milvus")
        return []

    if not utility.has_collection(collection_name):
        logger.info("Collection does not exist: %s", collection_name)
        return []

    collection = Collection(name=collection_name)
    collection.load()

    query_embedding = get_embedding([clean_query])
    if not query_embedding or not query_embedding[0]:
        logger.error("Failed to generate embedding")
        return []

    hits_attr = _search_one_field(
        collection=collection,
        query_embedding=query_embedding,
        anns_field=ATTR_VECTOR_FIELD,
        limit=candidate_k,
        expr=expr,
    )
    hits_value = _search_one_field(
        collection=collection,
        query_embedding=query_embedding,
        anns_field=VALUE_VECTOR_FIELD,
        limit=candidate_k,
        expr=expr,
    )
    logger.info(
        "Raw semantic hits: attr=%d value=%d",
        len(hits_attr),
        len(hits_value),
    )

    candidate_map: dict[str, Candidate] = {}

    def _candidate_key(entity: dict[str, Any]) -> str:
        return "||".join(
            [
                str(entity.get("source_node_id", "")),
                str(entity.get("destination_node_id", "")),
                str(entity.get("relation_name", "")),
                str(entity.get("attribute", "")),
                str(entity.get("attribute_value", "")),
            ]
        )

    for hit in hits_attr:
        score = float(hit.score)
        if score < similarity_threshold:
            continue
        entity = {field: hit.entity.get(field, "") for field in SEARCH_OUTPUT_FIELDS}
        key = _candidate_key(entity)
        if key not in candidate_map:
            candidate_map[key] = Candidate(
                source_node_id=str(entity.get("source_node_id", "")),
                destination_node_id=str(entity.get("destination_node_id", "")),
                relation_name=str(entity.get("relation_name", "")),
                attribute=str(entity.get("attribute", "")),
                attribute_value=str(entity.get("attribute_value", "")),
            )
        candidate_map[key].semantic_attr_raw = max(candidate_map[key].semantic_attr_raw, score)

    for hit in hits_value:
        score = float(hit.score)
        if score < similarity_threshold:
            continue
        entity = {field: hit.entity.get(field, "") for field in SEARCH_OUTPUT_FIELDS}
        key = _candidate_key(entity)
        if key not in candidate_map:
            candidate_map[key] = Candidate(
                source_node_id=str(entity.get("source_node_id", "")),
                destination_node_id=str(entity.get("destination_node_id", "")),
                relation_name=str(entity.get("relation_name", "")),
                attribute=str(entity.get("attribute", "")),
                attribute_value=str(entity.get("attribute_value", "")),
            )
        candidate_map[key].semantic_value_raw = max(candidate_map[key].semantic_value_raw, score)

    lexical_rows = _fetch_lexical_pool(
        collection=collection,
        limit=lexical_pool_limit,
        expr=expr,
    )
    logger.info("Lexical pool rows fetched: %d", len(lexical_rows))
    if lexical_rows:
        lexical_docs = [
            f"{row.get('relation_name', '')} {row.get('attribute', '')} {row.get('attribute_value', '')}".strip()
            for row in lexical_rows
        ]
        lexical_pool_bm25 = _bm25_scores(clean_query, lexical_docs)
        lexical_pool_overlap = [_lexical_overlap(clean_query, doc) for doc in lexical_docs]

        ranked_idx = sorted(
            range(len(lexical_rows)),
            key=lambda i: max(lexical_pool_bm25[i], lexical_pool_overlap[i]),
            reverse=True,
        )
        added_from_lexical = 0
        for idx in ranked_idx:
            if added_from_lexical >= lexical_top_k:
                break
            signal = max(lexical_pool_bm25[idx], lexical_pool_overlap[idx])
            if signal <= 0.0:
                break
            row = lexical_rows[idx]
            key = _candidate_key(row)
            if key not in candidate_map:
                candidate_map[key] = Candidate(
                    source_node_id=str(row.get("source_node_id", "")),
                    destination_node_id=str(row.get("destination_node_id", "")),
                    relation_name=str(row.get("relation_name", "")),
                    attribute=str(row.get("attribute", "")),
                    attribute_value=str(row.get("attribute_value", "")),
                )
                added_from_lexical += 1

        logger.info("Candidates added from lexical retrieval: %d", added_from_lexical)

    candidates = list(candidate_map.values())
    if not candidates:
        logger.info("No candidates above threshold %.3f", similarity_threshold)
        return []

    logger.info("Candidates collected before gate: %d", len(candidates))

    corpus = [f"{c.relation_name} {c.attribute} {c.attribute_value}".strip() for c in candidates]
    bm25_raw = _bm25_scores(clean_query, corpus)
    lexical_raw = [_lexical_overlap(clean_query, doc) for doc in corpus]
    semantic_raw = [max(c.semantic_attr_raw, c.semantic_value_raw) for c in candidates]

    bm25_norm = _normalize_scores(bm25_raw)
    lexical_norm = _normalize_scores(lexical_raw)
    semantic_norm = _normalize_scores(semantic_raw)

    lexical_signal = [max(bm25_norm[i], lexical_norm[i]) for i in range(len(candidates))]
    lexical_positive_count = sum(1 for s in lexical_signal if s >= min_lexical_signal)

    if require_lexical_signal:
        filtered: list[tuple[Candidate, float, float, float]] = []

        if lexical_positive_count > 0:
            logger.info(
                "Lexical signal detected (%d candidates). Apply strict lexical gate >= %.3f",
                lexical_positive_count,
                min_lexical_signal,
            )
            for idx, candidate in enumerate(candidates):
                if lexical_signal[idx] >= min_lexical_signal:
                    filtered.append((candidate, semantic_norm[idx], bm25_norm[idx], lexical_norm[idx]))
        else:
            logger.info(
                "No lexical signal detected. Apply semantic rescue with raw threshold >= %.3f",
                semantic_rescue_threshold,
            )
            for idx, candidate in enumerate(candidates):
                has_value_support = candidate.semantic_value_raw >= similarity_threshold
                if semantic_rescue_requires_value and not has_value_support:
                    continue
                if semantic_raw[idx] >= semantic_rescue_threshold:
                    filtered.append((candidate, semantic_norm[idx], bm25_norm[idx], lexical_norm[idx]))

        if not filtered:
            logger.warning(
                "All candidates failed lexical gate (min_lexical_signal=%.3f, semantic_rescue_threshold=%.3f). "
                "Likely query is not represented in this collection.",
                min_lexical_signal,
                semantic_rescue_threshold,
            )
            return []
        candidates = [item[0] for item in filtered]
        semantic_norm = [item[1] for item in filtered]
        bm25_norm = [item[2] for item in filtered]
        lexical_norm = [item[3] for item in filtered]
        logger.info("Candidates after lexical gate: %d", len(candidates))

    for idx, candidate in enumerate(candidates):
        candidate.bm25_score = bm25_norm[idx]
        candidate.lexical_score = lexical_norm[idx]
        candidate.semantic_score = semantic_norm[idx]
        candidate.final_score = (
            semantic_weight * candidate.semantic_score
            + bm25_weight * candidate.bm25_score
            + lexical_weight * candidate.lexical_score
        )

    candidates.sort(key=lambda c: c.final_score, reverse=True)
    top_candidates = candidates[:top_k]

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

    logger.info("Hybrid candidates after fusion: %d", len(candidates))
    for idx, item in enumerate(output[:5], 1):
        logger.info(
            "#%d final=%.4f semantic=%.4f bm25=%.4f lexical=%.4f relation=%s attr=%s",
            idx,
            item["final_score"],
            item["semantic_score"],
            item["bm25_score"],
            item["lexical_score"],
            item["relation_name"],
            item["attribute"],
        )

    return output


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run hybrid full text + semantic search on Milvus")
    parser.add_argument("query", nargs="?", help="Input query string")
    parser.add_argument("--collection", default=DEFAULT_COLLECTION, help="Milvus collection name")
    parser.add_argument("--top-k", type=int, default=10, help="Final output size")
    parser.add_argument("--candidate-k", type=int, default=80, help="Semantic candidate size per vector field")
    parser.add_argument("--lexical-pool-limit", type=int, default=10000, help="Rows fetched for lexical retrieval")
    parser.add_argument("--lexical-top-k", type=int, default=300, help="Top lexical candidates to merge")
    parser.add_argument("--threshold", type=float, default=0.35, help="Minimum semantic raw score")
    parser.add_argument("--semantic-weight", type=float, default=0.7, help="Fusion weight for semantic score")
    parser.add_argument("--bm25-weight", type=float, default=0.2, help="Fusion weight for BM25 score")
    parser.add_argument("--lexical-weight", type=float, default=0.1, help="Fusion weight for lexical overlap score")
    parser.add_argument(
        "--min-lexical-signal",
        type=float,
        default=0.02,
        help="Minimum lexical signal to keep candidate when lexical gate is enabled",
    )
    parser.add_argument(
        "--semantic-rescue-threshold",
        type=float,
        default=0.6,
        help="Allow semantic-strong candidates (raw semantic score) to pass lexical gate when lexical has no signal",
    )
    parser.add_argument(
        "--semantic-rescue-any-field",
        action="store_true",
        help="Allow semantic rescue from either vector field (default requires value/content field support)",
    )
    parser.add_argument(
        "--disable-lexical-gate",
        action="store_true",
        help="Disable lexical gate and allow semantic-only candidates",
    )
    parser.add_argument("--expr", default=None, help="Optional Milvus filter expression")
    return parser


def main() -> None:
    args = _build_arg_parser().parse_args()
    query = args.query or input("Enter query: ").strip()

    results = full_text_search(
        query=query,
        collection_name=args.collection,
        top_k=args.top_k,
        candidate_k=args.candidate_k,
        lexical_pool_limit=args.lexical_pool_limit,
        lexical_top_k=args.lexical_top_k,
        similarity_threshold=args.threshold,
        semantic_weight=args.semantic_weight,
        bm25_weight=args.bm25_weight,
        lexical_weight=args.lexical_weight,
        min_lexical_signal=args.min_lexical_signal,
        semantic_rescue_threshold=args.semantic_rescue_threshold,
        semantic_rescue_requires_value=not args.semantic_rescue_any_field,
        require_lexical_signal=not args.disable_lexical_gate,
        expr=args.expr,
    )
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
