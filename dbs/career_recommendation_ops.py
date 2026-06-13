import ast
import json
import re
import unicodedata
from typing import Any

from pymilvus import Collection, utility

from dbs.milvus_helper import connect_milvus, get_embedding, search_entity_by_name, search_similar_documents
from utils.logging_config import get_logger

logger = get_logger(__name__)

_COLLECTION_NAME = "career_recommendation"


def _normalize_text(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for item in items:
        clean_item = (item or "").strip()
        if not clean_item:
            continue
        marker = clean_item.casefold()
        if marker in seen:
            continue
        seen.add(marker)
        deduped.append(clean_item)
    return deduped


def _parse_keywords(raw_value: Any) -> list[str]:
    if isinstance(raw_value, list):
        return _dedupe_keep_order([str(item).strip() for item in raw_value if str(item).strip()])

    if not isinstance(raw_value, str) or not raw_value.strip():
        return []

    stripped = raw_value.strip()
    for parser in (json.loads, ast.literal_eval):
        try:
            parsed = parser(stripped)
            if isinstance(parsed, list):
                return _dedupe_keep_order([str(item).strip() for item in parsed if str(item).strip()])
        except (json.JSONDecodeError, SyntaxError, ValueError):
            continue

    return _dedupe_keep_order([part.strip() for part in stripped.split(",") if part.strip()])


def _match_keywords(record_keywords: list[str], query_terms: list[str], normalized_query_text: str) -> list[str]:
    matched: list[str] = []
    normalized_terms = [_normalize_text(term) for term in query_terms if _normalize_text(term)]

    for keyword in record_keywords:
        normalized_keyword = _normalize_text(keyword)
        if not normalized_keyword:
            continue

        if normalized_keyword in normalized_query_text:
            matched.append(keyword)
            continue

        if any(
            normalized_term in normalized_keyword or normalized_keyword in normalized_term
            for normalized_term in normalized_terms
        ):
            matched.append(keyword)

    return _dedupe_keep_order(matched)


def merge_majors(potential_majors: list[dict[str, str]], raw_results: list[dict[str, Any]]) -> list[dict[str, float]]:
    a_names = {name for item in potential_majors for _, name in item.items()}

    b_items = []
    for item in raw_results:
        major = item.get("major")
        if not major:
            continue

        try:
            score = float(item.get("score", 0.0))
        except (TypeError, ValueError):
            score = 0.0

        b_items.append({
            "major": major,
            "score": score
        })

    result = []

    # Có cả potential_majors và raw_results => score = 1
    for item in b_items:
        if item["major"] in a_names:
            result.append({
                "major": item["major"],
                "score": 1.0
            })

    # Nếu potential_majors >= 3 thì trả về toàn bộ phần tử cùng tồn tại
    if len(potential_majors) >= 3:
        return result

    needed = 3 - len(result)

    # Lấy thêm từ raw_results score cao nhất, không trùng với potential_majors
    remaining = [
        item for item in b_items
        if item["major"] not in a_names
    ]

    remaining.sort(key=lambda x: x["score"], reverse=True)

    result.extend(remaining[:needed])

    return result


def search_career_recommendation_candidates(
    original_query: str,
    keywords: list[str] | None = None,
    top_k: int = 5,
    threshold: float = 0.35,
    collection_name: str = _COLLECTION_NAME,
    potential_majors: list[dict[Any,str]] | None = None,
) -> list[dict[str, Any]]:
    # query_terms = _dedupe_keep_order([*(keywords or []), original_query])
    query_terms = ','.join(keywords) if keywords else None
    if not query_terms:
        return []
    logger.info(f"search_career_recommendation_candidates query_terms: {query_terms}")
    if not connect_milvus():
        logger.warning("search_career_recommendation_candidates: Milvus unavailable")
        return []

    if not utility.has_collection(collection_name):
        logger.warning("search_career_recommendation_candidates: collection '%s' does not exist", collection_name)
        return []

    major_limit = 100 if potential_majors else top_k

    raw_results = search_similar_documents(
        collection_name=collection_name,
        query=[query_terms],
        output_fields=["major", "keywords"],
        top_k=major_limit,
        threshold=threshold,
    )

    formatted_results: list[dict[str, Any]] = []

    for r in raw_results:

        formatted_results.append(
            {
                "major": r["major"],
                # "keywords": list(r.get("keywords", []) or []),
                "score": float(r.get("score", 0.0)),
            }
        )
    logger.info(f"search_career_recommendation_candidates formatted_results: {formatted_results}")

    final_results: list[dict[str, Any]] = merge_majors(potential_majors, formatted_results) if potential_majors else formatted_results

    return final_results


def resolve_career_recommendation_candidates(
    candidates: list[dict[str, Any]] | None,
    time: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    resolved: list[dict[str, Any]] = []
    majors: list[str] = []
    logger.info(f"resolve_career_recommendation_candidates candidates: {candidates}")

    for candidate in candidates or []:
        major_name = str(candidate.get("major", "")).strip()
        logger.info(f"resolve_career_recommendation_candidates major_name: {major_name}")
        if not major_name:
            continue
        majors.append(major_name)
    logger.info(f"resolve_career_recommendation_candidates majors: {majors}")
    expr_type = "node_type in ['Major', 'Specialization']"
    expr_time = ""
    if time:
        from_year = str(time.get("from_year"))
        to_year = time.get("to_year", "")
        logger.debug("milvus search_entity_by_name time filter: from_year=%s, to_year=%s", from_year, to_year)
        if to_year is not None:
            to_year = str(int(to_year) + 1)
        if from_year and to_year:
            expr_time = f"(effective_from > '{from_year}' || effective_from == '') && (effective_to < '{to_year}'  || effective_to == '')"
        elif from_year:
            expr_time = f"effective_from > '{from_year}'  || effective_from == ''"
        elif to_year:
            expr_time = f"effective_to < '{to_year}' || effective_to == ''"
        else:
            expr_time = None
    active_expr = "is_active == true"
    expr = f"({expr_type}) and ({expr_time}) and ({active_expr})" if expr_time else f"({expr_type}) and ({active_expr})"

    search_results = search_similar_documents(
        collection_name="knowledge_university_entity",
        query=majors,
        output_fields=[
            "node_id",
            "node_name",
            "node_type",
            "attribute_keys",
            "description",
            # "keywords",
        ],
        top_k=1,
        threshold=0.55,
        expr=expr,
    )
    logger.info(f"search_career_recommendation_candidates search_results: {search_results}")

    for r in search_results:
        attribute_keys = r.get("attribute_keys", [])

        # Force convert protobuf to list
        if hasattr(attribute_keys, "__iter__") and not isinstance(attribute_keys, (str, dict)):
            attribute_keys = list(attribute_keys)  # Force to Python list

        resolved.append(
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

    return resolved


def build_career_recommendation_list_results(
    candidates: list[dict[str, Any]] | None,
    resolved_candidates: list[dict[str, Any]] | None,
    attribute_map: dict[str, dict[str, Any]] | None,
    original_query: str,
    user_keywords: list[str] | None = None,
    target_topics: list[str] | None = None,
    display_limit: int = 3,
    start_index: int = 0,
) -> dict[str, Any] | None:
    if not resolved_candidates:
        return None

    attribute_map = attribute_map or {}
    allowed_types = {str(topic).strip() for topic in (target_topics or []) if str(topic).strip()}
    if allowed_types & {"Major", "Specialization"}:
        # In recommendation mode, majors and specializations are both valid study directions.
        allowed_types |= {"Major", "Specialization"}
    items: list[dict[str, Any]] = []

    for candidate in resolved_candidates:
        node_type = str(candidate.get("node_type") or "").strip()
        if allowed_types and node_type and node_type not in allowed_types:
            continue

        node_id = str(candidate.get("node_id", "")).strip()
        attrs = attribute_map.get(node_id, {}) if node_id else {}
        item = {
            "id": node_id or None,
            "name": attrs.get("name") or candidate.get("node_name") or candidate.get("major"),
            "type": node_type or "Major",
            "description": attrs.get("description") or candidate.get("description"),
            "career_opportunities": attrs.get("career_opportunities"),
            "objective": attrs.get("objective"),
            "key_points": attrs.get("key_points"),
            "matched_keywords": candidate.get("matched_keywords") or [],
            "keyword_samples": candidate.get("candidate_keywords") or [],
        }
        items.append({key: value for key, value in item.items() if value not in (None, "", [], {})})

    if not items:
        return None

    total = len(items)
    effective_limit = max(1, int(display_limit or 3))
    safe_start_index = max(0, int(start_index or 0))
    merged_keywords = _dedupe_keep_order(
        [*(user_keywords or []), *[keyword for item in items for keyword in item.get("matched_keywords", [])]]
    )

    return {
        "items": items,
        "total": total,
        "display_limit": effective_limit,
        "start_index": safe_start_index,
        "list_last_index": min(safe_start_index + effective_limit, total),
        "source": "career_recommendation",
        "selection_mode": "career_recommendation",
        "recommendation_query": original_query,
        "matched_keywords": merged_keywords[:8],
        "current_primary_entity": {},
        "raw_candidates": candidates or [],
    }
