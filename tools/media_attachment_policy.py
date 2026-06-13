import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

MAJOR_MEDIA_SENT_KEY = "sent_major_media_keys"
MAJOR_NODE_TYPES = {"Major", "Specialization"}
REPEATABLE_MEDIA_NODE_TYPES = {"Campus", "Facility", "Activity", "Club"}


def _normalize_key(value: Any) -> str:
    if not isinstance(value, str):
        value = str(value or "")
    text = value.strip().casefold()
    text = re.sub(r"^(ngành|chuyên ngành)\s+", "", text)
    return re.sub(r"\s+", " ", text)


def _major_item_key(code: Any, name: Any) -> str:
    code_text = _normalize_key(code)
    name_text = _normalize_key(name)
    return f"{code_text}:{name_text}"


def _major_node_key(node_id: Any) -> str:
    return f"node:{str(node_id or '').strip()}"


def _iter_major_items(raw_items: Any) -> list[tuple[str, str, str]]:
    items: list[tuple[str, str, str]] = []
    if not isinstance(raw_items, list):
        return items

    for item in raw_items:
        if not isinstance(item, dict):
            continue
        for code, name in item.items():
            name_text = str(name or "").strip()
            if not name_text:
                continue
            items.append((str(code or "UNKNOWN"), name_text, _major_item_key(code, name_text)))
    return items


def _get_state_dict(context: Any) -> dict[str, Any]:
    state = getattr(context, "state", context)
    if hasattr(state, "to_dict"):
        return state.to_dict()
    return state if isinstance(state, dict) else {}


def _get_mutable_state(context: Any) -> Any:
    state = getattr(context, "state", context)
    return state if hasattr(state, "get") and hasattr(state, "__setitem__") else {}


def _set_state_value(context: Any, key: str, value: Any) -> None:
    state = _get_mutable_state(context)
    state[key] = value


def _merge_attachments_into_context(context: Any, attachments: list[dict[str, Any]]) -> None:
    if not attachments:
        return

    state = _get_mutable_state(context)
    extra_data = state.get("extra_data", {})
    if not isinstance(extra_data, dict):
        extra_data = {}

    existing = extra_data.get("attachments") or []
    if not isinstance(existing, list):
        existing = []

    merged: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for attachment in [*existing, *attachments]:
        if not isinstance(attachment, dict):
            continue
        url = attachment.get("url")
        marker = str(url or attachment)
        if marker in seen_urls:
            continue
        seen_urls.add(marker)
        merged.append(attachment)

    extra_data["attachments"] = merged
    state["extra_data"] = extra_data
    state["has_media"] = bool(merged)


def _fetch_media(
    node_ids: list[str],
    min_sample: int = 2,
    max_sample: int = 5,
    randomize: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    unique_node_ids = [node_id for node_id in dict.fromkeys(node_ids) if isinstance(node_id, str) and node_id]
    if not unique_node_ids:
        return {"attachments": []}

    try:
        from dbs.mongo_helper import fetch_media_for_entities

        return fetch_media_for_entities(
            node_ids=unique_node_ids,
            min_sample=min_sample,
            max_sample=max_sample,
            randomize=randomize,
        )
    except Exception as exc:
        logger.error("Failed to fetch media attachments: %s", exc)
        return {"attachments": []}


def _resolve_major_nodes(pending_majors: list[tuple[str, str, str]]) -> list[dict[str, str]]:
    try:
        from dbs.milvus_helper import search_entity_by_name
    except Exception as exc:
        logger.error("Failed to import major resolver for media: %s", exc)
        return []

    resolved_nodes: list[dict[str, str]] = []
    seen_node_ids: set[str] = set()
    for _, name, key in pending_majors:
        try:
            results = search_entity_by_name(
                entity_name=[name],
                entity_type=["Major", "Specialization"],
                top_k=1,
                threshold=0.6,
            )
        except Exception as exc:
            logger.error("Failed to resolve interested major '%s' for media: %s", name, exc)
            continue

        for result in results or []:
            node_type = result.get("node_type")
            node_id = result.get("node_id")
            if node_type not in MAJOR_NODE_TYPES or not isinstance(node_id, str) or not node_id:
                continue
            if node_id in seen_node_ids:
                continue
            seen_node_ids.add(node_id)
            resolved_nodes.append(
                {
                    "node_id": node_id,
                    "item_key": key,
                    "node_key": _major_node_key(node_id),
                }
            )

    return resolved_nodes


def fetch_interested_major_media_once(context: Any) -> dict[str, list[dict[str, Any]]]:
    """Fetch media once per interested major in a session."""
    # Major/Specialization media is paused until trigger rules are finalized.
    return {"attachments": []}

    state = _get_state_dict(context)
    user_state = state.get("user_state") or {}
    interested_majors = _iter_major_items(user_state.get("interested_majors"))
    if not interested_majors:
        return {"attachments": []}

    sent_keys = state.get(MAJOR_MEDIA_SENT_KEY) or []
    if not isinstance(sent_keys, list):
        sent_keys = []
    sent_set = set(str(key) for key in sent_keys)

    pending_majors = [(code, name, key) for code, name, key in interested_majors if key not in sent_set]
    if not pending_majors:
        return {"attachments": []}

    resolved_nodes = _resolve_major_nodes(pending_majors)
    if not resolved_nodes:
        return {"attachments": []}

    already_sent_item_keys = [node["item_key"] for node in resolved_nodes if node["node_key"] in sent_set]
    major_node_ids = [node["node_id"] for node in resolved_nodes if node["node_key"] not in sent_set]
    if not major_node_ids:
        if already_sent_item_keys:
            _set_state_value(context, MAJOR_MEDIA_SENT_KEY, sorted(sent_set.union(already_sent_item_keys)))
        return {"attachments": []}

    media = _fetch_media(major_node_ids, min_sample=0, max_sample=5, randomize=False)
    attachments = media.get("attachments") or []
    if attachments:
        sent_major_keys = {
            key
            for node in resolved_nodes
            if node["node_id"] in major_node_ids
            for key in (node["item_key"], node["node_key"])
        }
        _set_state_value(context, MAJOR_MEDIA_SENT_KEY, sorted(sent_set.union(sent_major_keys)))
        _merge_attachments_into_context(context, attachments)
    return media


def fetch_media_for_query_plan_nodes(
    primary_found_list: list[dict[str, Any]] | None = None,
    context_found_list: list[dict[str, Any]] | None = None,
    primary_topic: str | None = None,
    subtopics: list[str] | None = None,
    sent_major_media_keys: list[str] | None = None,
) -> dict[str, Any]:
    """Fetch media for resolved WHAT attribute/relation nodes."""
    sent_keys = sent_major_media_keys if isinstance(sent_major_media_keys, list) else []
    sent_set = set(str(key) for key in sent_keys)
    subtopics = subtopics if isinstance(subtopics, list) else []
    # Major/Specialization media is paused until trigger rules are finalized.
    include_major_media = False

    node_ids: list[str] = []
    sent_major_keys: set[str] = set()
    for found_list in (primary_found_list, context_found_list):
        for node in found_list or []:
            if not isinstance(node, dict):
                continue
            node_type = node.get("node_type")
            node_id = node.get("node_id")
            if not isinstance(node_id, str) or not node_id:
                continue
            # if node_type in REPEATABLE_MEDIA_NODE_TYPES:
            node_ids.append(node_id)
            # elif include_major_media and node_type in MAJOR_NODE_TYPES:
            #     node_key = _major_node_key(node_id)
            #     if node_key not in sent_set:
            #         node_ids.append(node_id)
            #         sent_major_keys.add(node_key)

    media = _fetch_media(node_ids, min_sample=0, max_sample=5, randomize=False)
    # if media.get("attachments") and sent_major_keys:
    #     media[MAJOR_MEDIA_SENT_KEY] = sorted(sent_set.union(sent_major_keys))
    return media
