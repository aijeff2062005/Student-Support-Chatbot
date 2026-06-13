"""Province Normalization — leverages Milvus `Province_search.province_mapping`.

The Milvus table stores 63 rows. Each row maps:
- `province_63` (current official province name)
- `province_34` (legacy province name)

`normalize_province`: any input -> `province_63`
`get_province_34`: `province_63` -> `province_34`
"""

import logging
import threading
import traceback
import unicodedata

from pymilvus import Collection, connections, utility

from dbs.milvus_helper import MILVUS_HOST, MILVUS_PASSWORD, MILVUS_PORT, MILVUS_USER

logger = logging.getLogger(__name__)

DB_NAME = "Province_search"
COLLECTION_NAME = "province_mapping"
CONNECTION_ALIAS = "conn_Province_search"

_cache_lock = threading.Lock()
_province_63_lower: dict[str, str] | None = None  # p63 lower -> p63 official
_province_63_to_34: dict[str, str] | None = None  # p63 lower -> p34 official
_folded_map: dict[str, str] | None = None  # folded -> p63 official

_PREFIXES = ("thành phố ", "tp. ", "tp ", "tỉnh ")


def _fold(text: str) -> str:
    if not text:
        return ""
    text = text.lower().replace("đ", "d")
    return "".join(ch for ch in unicodedata.normalize("NFD", text) if unicodedata.category(ch) != "Mn")


def _strip_prefix(val: str) -> str:
    lower = val.lower().strip()
    for pf in _PREFIXES:
        if lower.startswith(pf):
            return val[len(pf) :].strip()
    return val.strip()


def _get_collection() -> Collection | None:
    try:
        if not connections.has_connection(CONNECTION_ALIAS):
            connections.connect(
                alias=CONNECTION_ALIAS,
                host=MILVUS_HOST,
                port=MILVUS_PORT,
                user=MILVUS_USER,
                password=MILVUS_PASSWORD,
                db_name=DB_NAME,
            )
        if not utility.has_collection(COLLECTION_NAME, using=CONNECTION_ALIAS):
            return None
        col = Collection(name=COLLECTION_NAME, using=CONNECTION_ALIAS)
        col.load()
        return col
    except Exception:
        traceback.print_exc()
        return None


def _ensure_cache() -> bool:
    global _province_63_lower, _province_63_to_34, _folded_map
    with _cache_lock:
        if _province_63_lower is not None:
            return True
        col = _get_collection()
        if col is None:
            return False
        rows = col.query(expr='id != ""', output_fields=["province_63", "province_34"], limit=100)

        p63_map: dict[str, str] = {}
        p63_to_34: dict[str, str] = {}
        folded: dict[str, str] = {}

        for r in rows:
            p63 = r.get("province_63", "").strip()
            p34 = r.get("province_34", "").strip()
            if p63:
                p63_map[p63.lower()] = p63
            if p63 and p34:
                p63_to_34[p63.lower()] = p34
                folded[_fold(p34)] = p63

        for r in rows:
            p63 = r.get("province_63", "").strip()
            if p63:
                folded[_fold(p63)] = p63

        _province_63_lower = p63_map
        _province_63_to_34 = p63_to_34
        _folded_map = folded
        logger.info(f"Province cache: {len(p63_map)} p63, {len(p63_to_34)} p63->p34, {len(folded)} folded")
        return True


def _like_search(raw: str) -> str | None:
    """Run a LIKE search against both `province_63` and `province_34`."""
    col = _get_collection()
    if col is None:
        return None
    escaped = raw.replace('"', '\\"')
    for field in ("province_63", "province_34"):
        results = col.query(
            expr=f'{field} like "%{escaped}%"',
            output_fields=["province_63"],
            limit=1,
        )
        if results:
            val = results[0].get("province_63", "").strip()
            if val:
                return val
    return None


def normalize_province(raw_value: str | None) -> str | None:
    """Normalize any province input value to official `province_63`."""
    if not raw_value or not isinstance(raw_value, str) or not raw_value.strip():
        return raw_value

    raw_value = raw_value.strip()
    if not _ensure_cache() or _province_63_lower is None or _folded_map is None:
        return raw_value
    province_63_lower = _province_63_lower
    folded_map = _folded_map

    lower = raw_value.lower()
    stripped = _strip_prefix(raw_value).lower()

    # 1. Exact match province_63
    if lower in province_63_lower:
        return province_63_lower[lower]
    if stripped != lower and stripped in province_63_lower:
        return province_63_lower[stripped]

    folded = _fold(raw_value)
    if folded in folded_map:
        return folded_map[folded]
    folded_stripped = _fold(stripped)
    if folded_stripped != folded and folded_stripped in folded_map:
        return folded_map[folded_stripped]

    try:
        result = _like_search(raw_value)
        if result:
            return result
    except Exception as e:
        logger.error(f"LIKE search failed for '{raw_value}': {e}")

    logger.warning(f"Could not normalize province: '{raw_value}'")
    return raw_value


def get_province_34(province_63: str | None) -> str | None:
    """Map `province_63` to `province_34`. Returns None when not found."""
    if not province_63 or not isinstance(province_63, str):
        return None
    if not _ensure_cache() or _province_63_to_34 is None:
        return None
    return _province_63_to_34.get(province_63.lower().strip())
