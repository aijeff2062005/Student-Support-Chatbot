"""Helpers for support-domain list query plans."""

from __future__ import annotations

import copy
import re
import unicodedata
from typing import Any

from utils.logging_config import get_logger

logger = get_logger(__name__)


_GENERIC_QUERY_TERMS = {
    "co",
    "cac",
    "nhung",
    "loai",
    "nao",
    "gi",
    "danh",
    "sach",
    "liet",
    "ke",
    "chinh",
    "sach",
    "quy",
    "dinh",
    "ho",
    "tro",
    "sinh",
    "vien",
    "truong",
    "gdu",
    "cua",
    "ve",
    "cho",
    "toi",
    "em",
    "minh",
}


def _normalize_text(value: Any) -> str:
    text = str(value or "").casefold()
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text)).strip()


def _flatten_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        parts: list[str] = []
        for key, child in value.items():
            parts.append(str(key))
            parts.extend(_flatten_values(child))
        return parts
    if isinstance(value, (list, tuple, set)):
        parts = []
        for item in value:
            parts.extend(_flatten_values(item))
        return parts
    return [str(value)]


def _item_search_text(item: dict[str, Any]) -> str:
    parts = [
        item.get("name"),
        item.get("description"),
        " ".join(item.get("labels") or []),
    ]
    parts.extend(_flatten_values(item.get("properties") or {}))
    return _normalize_text(" ".join(str(part or "") for part in parts))


def _build_filter_terms(keywords: list[str] | None, keyword_attributes: list[str] | None) -> list[str]:
    raw_terms = [*(keywords or []), *(keyword_attributes or [])]
    terms: list[str] = []
    seen: set[str] = set()
    for raw in raw_terms:
        normalized = _normalize_text(raw)
        if not normalized:
            continue
        tokens = [token for token in normalized.split() if token not in _GENERIC_QUERY_TERMS]
        if not tokens:
            continue
        candidates = [normalized]
        if len(tokens) <= 4:
            candidates.append(" ".join(tokens))
        for candidate in candidates:
            candidate = candidate.strip()
            if candidate and candidate not in seen:
                seen.add(candidate)
                terms.append(candidate)
    return terms


def filter_support_list_results(
    list_results: dict[str, Any] | None,
    keywords: list[str] | None = None,
    keyword_attributes: list[str] | None = None,
    target_topics: list[str] | None = None,
) -> dict[str, Any] | None:
    """Filter broad support lists by specific query terms without forcing empty results."""
    if not isinstance(list_results, dict):
        return list_results

    items = list_results.get("items") or []
    if not isinstance(items, list) or not items:
        return list_results

    terms = _build_filter_terms(keywords, keyword_attributes)
    if not terms:
        return list_results

    filtered = []
    for item in items:
        if not isinstance(item, dict):
            continue
        search_text = _item_search_text(item)
        if any(term in search_text for term in terms):
            filtered.append(item)

    if not filtered:
        logger.info("filter_support_list_results: no keyword matches for terms=%s; keeping original list", terms)
        return list_results

    result = copy.deepcopy(list_results)
    result["items"] = filtered
    result["total"] = len(filtered)
    display_limit = int(result.get("display_limit") or len(filtered))
    start_index = int(result.get("start_index") or 0)
    result["list_last_index"] = min(start_index + display_limit, len(filtered))
    result["source"] = "support_keyword_filter"
    logger.info(
        "filter_support_list_results: targets=%s terms=%s %s->%s",
        target_topics,
        terms,
        len(items),
        len(filtered),
    )
    return result
