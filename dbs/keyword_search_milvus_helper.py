from __future__ import annotations

import hashlib
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pymilvus import Collection

from dbs.milvus_helper import connect_milvus, get_embedding
from utils.logging_config import get_logger

logger = get_logger(__name__)

# =========================
# Public types
# =========================


@dataclass(frozen=True)
class Record:
    id: str
    content: str
    metadata: dict[str, Any]
    score: float


@dataclass(frozen=True)
class BestResult:
    record: list[Record]


# =========================
# Text + scoring utils
# =========================

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+", re.UNICODE)


def _normalize(q: str) -> str:
    return re.sub(r"\s+", " ", q.strip())


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text)]


def _extract_keywords(q: str) -> list[str]:
    seen, out = set(), []
    for t in _tokenize(q):
        if len(t) >= 2 and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _minmax(scores: list[float]) -> list[float]:
    if not scores:
        return []
    mn, mx = min(scores), max(scores)
    if math.isclose(mn, mx):
        return [0.5] * len(scores)
    return [(s - mn) / (mx - mn) for s in scores]


def _hash(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()


def _bm25_lite(keywords: list[str], content: str) -> float:
    if not keywords:
        return 0.0
    tokens = _tokenize(content)
    tf = {}
    for t in tokens:
        tf[t] = tf.get(t, 0) + 1

    score = 0.0
    dl = max(len(tokens), 1)
    for kw in keywords:
        f = tf.get(kw, 0)
        if f > 0:
            score += (f * 2.2) / (f + 1.2 * (1 + 0.75 * dl / 200))
    return score


# =========================
# Milvus Hybrid Util
# =========================


class MilvusHybridUtil:
    """
    Milvus-only hybrid retrieval:
    vector search -> keyword re-score on `content` -> pick best record
    """

    def __init__(
        self,
        *,
        collection: Any,
        embed_fn: Callable[[str], list[float]],
        vector_field: str = "embedding",
        id_field: str = "node_id",
        metric: str = "COSINE",
        top_k: int = 80,
        w_vec: float = 1,
        w_kw: float = 0.3,
        cache_ttl: int = 30,
    ):
        self.collection = collection
        self.embed_fn = embed_fn
        self.vector_field = vector_field
        self.id_field = id_field
        self.metric = metric.upper()
        self.top_k = top_k
        self.w_vec = w_vec
        self.w_kw = w_kw
        self.cache_ttl = cache_ttl
        self._cache: dict[str, tuple[float, BestResult]] = {}

    def get_best(self, question: str, *, filters: str | None = None) -> BestResult | None:
        q = _normalize(question)
        key = _hash(f"{q}|{filters}")

        now = time.time()
        if key in self._cache:
            ts, cached = self._cache[key]
            if now - ts < self.cache_ttl:
                return cached

        emb = self.embed_fn(q)

        hits = self.collection.search(
            data=emb,
            anns_field=self.vector_field,
            limit=self.top_k,
            param={"metric_type": self.metric, "params": {"nprobe": 16}},
            expr=filters,
            output_fields=[self.id_field, "content", "node_id", "attribute_name", "node_type"],
            # output_fields=[self.id_field, "content", "node_id", "node_type"],
        )

        if not hits or not hits[0]:
            return None

        records = []
        for h in hits[0]:
            ent = h.entity
            raw = float(h.score)
            vec_score = raw if self.metric in ("COSINE", "IP") else -raw

            records.append(
                (
                    Record(
                        id=str(ent.get(self.id_field)),
                        content=str(ent.get("content")),
                        metadata={
                            "node_id": ent.get("node_id"),
                            "attribute": ent.get("attribute_name"),
                            "node_type": ent.get("node_type"),
                        },
                        score=vec_score,
                    ),
                    vec_score,
                )
            )

        vec_norm = [s for _, s in records]
        logger.debug("vec_norm: %s", vec_norm)

        filtered_records: list[Record] = []

        for (rec, _), v in zip(records, vec_norm, strict=False):
            fused = self.w_vec * v
            # print(f"fused: {fused}")
            if fused > 0.7:
                filtered_records.append(rec)

        best = BestResult(
            record=filtered_records,
        )

        self._cache[key] = (now, best)
        return best


connect_milvus()
col = Collection("knowledge_university_chunk")
util = MilvusHybridUtil(
    collection=col,
    embed_fn=lambda text: get_embedding([text])[0],  # embedding local, KHÔNG LLM
)
