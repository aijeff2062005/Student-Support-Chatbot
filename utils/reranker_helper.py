"""Reranker Service using Qwen3-Reranker-8B via LiteLLM proxy."""

import logging
from typing import Any

import litellm

from configs.llm_client import get_rerank_params

logger = logging.getLogger(__name__)


def rerank_relationships(
    query: str, candidate_relations: list[str], top_k: int = 3, threshold: float = 0.7, api_key: str | None = None
) -> list[dict[str, Any]]:
    if not candidate_relations:
        logger.warning("No candidate relations provided for reranking")
        return []

    try:
        logger.info(f"Reranking {len(candidate_relations)} relationships for query: '{query}'")

        response = litellm.rerank(
            **get_rerank_params(),
            query=query,
            documents=candidate_relations,
            top_n=len(candidate_relations),
        )

        scores = [0.0] * len(candidate_relations)
        for item in response.results:
            scores[item["index"]] = float(item["relevance_score"])

        logger.info(f"Parsed {len(scores)} scores: {scores}")

        scored_relations: list[tuple[str, float]] = [
            (rel, float(scores[idx])) for idx, rel in enumerate(candidate_relations)
        ]
        scored_relations.sort(key=lambda item: item[1], reverse=True)
        filtered_results = [
            {"relation": relation, "score": score} for relation, score in scored_relations if score >= threshold
        ][:top_k]

        logger.info(f"Reranking complete: {len(filtered_results)} relations above threshold {threshold}")
        return filtered_results

    except Exception as e:
        logger.error(f"Reranker error: {e}", exc_info=True)
        return [{"relation": rel, "score": 0.0} for rel in candidate_relations[:top_k]]


def rerank_industry(
    query: str,
    candidate_industries: list[str],
) -> dict[str, Any]:
    """Rerank candidate industries using Qwen3-Reranker-8B."""
    if not candidate_industries:
        return {}

    try:
        response = litellm.rerank(
            **get_rerank_params(),
            query=query,
            documents=candidate_industries,
            top_n=len(candidate_industries),
        )

        scores = [0.0] * len(candidate_industries)
        for item in response.results:
            scores[item["index"]] = float(item["relevance_score"])

        results = [{"industry": ind, "score": scores[idx]} for idx, ind in enumerate(candidate_industries)]
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[0]

    except Exception as e:
        logger.error(f"Reranker error: {e}", exc_info=True)
        return {}


def deduplicate_relationships(relationships_by_node: dict[str, list[str]]) -> list[str]:
    """Deduplicate relationships from multiple nodes."""
    all_relations = []
    for relations in relationships_by_node.values():
        all_relations.extend(relations)

    seen = set()
    unique_relations = []
    for rel in all_relations:
        if rel not in seen:
            seen.add(rel)
            unique_relations.append(rel)

    logger.info(f"Deduplicated: {len(all_relations)}  {len(unique_relations)} unique relationships")
    return unique_relations
