from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class WhatQuestionFamily(StrEnum):
    """Supported question family for canonical WHAT query results."""

    WHAT = "what"


class WhatIntent(StrEnum):
    """Canonical WHAT intent values exposed to downstream consumers."""

    ATTRIBUTES = "attributes"
    COMPARE = "compare"
    RELATION = "relation"
    LIST = "list"
    COUNT = "count"
    CONSTRAINT_LIST = "constraint-list"
    UNKNOWN = "unknown"


class WhatStatus(StrEnum):
    """Execution status of the final canonical result."""

    OK = "ok"
    FALLBACK = "fallback"
    EMPTY = "empty"


class WhatAnswerKind(StrEnum):
    """High-level answer shape used by downstream answer prompting."""

    ATTRIBUTES = "attributes"
    COMPARISON = "comparison"
    RELATION = "relation"
    COLLECTION = "collection"


class WhatSource(StrEnum):
    """Origin of the answer evidence after normalization."""

    GRAPH = "graph"
    HYBRID = "hybrid"
    CRAWLED = "crawled"


class WhatEntityRef(BaseModel):
    """Resolved entity reference that is safe to expose in the chatbot contract."""

    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(default=None, description="Canonical graph node id if the entity was resolved.")
    name: str | None = Field(default=None, description="Resolved display name of the entity.")
    type: str | None = Field(default=None, description="Resolved graph node label of the entity.")
    description: str | None = Field(default=None, description="Short resolved description when available.")
    score: float | None = Field(default=None, description="Resolution similarity score when available.")

    @field_validator("score")
    @classmethod
    def validate_score(cls, value: float | None) -> float | None:
        if value is None:
            return value
        # if not 0 <= value <= 1:
        #     raise ValueError("Entity score must be between 0 and 1.")
        return value


class WhatEntities(BaseModel):
    """Resolved entities grouped by their conversational role."""

    model_config = ConfigDict(extra="forbid")

    primary: list[WhatEntityRef] = Field(default_factory=list, description="Resolved primary entities asked about.")
    context: list[WhatEntityRef] = Field(default_factory=list, description="Resolved context/scope entities.")
    compare: list[WhatEntityRef] = Field(default_factory=list, description="Resolved compare-side entities.")


class WhatAnswer(BaseModel):
    """Canonical answer payload consumed by the response agent."""

    model_config = ConfigDict(extra="forbid")

    kind: WhatAnswerKind = Field(description="Stable answer kind for downstream routing and prompting.")
    data: dict[str, Any] = Field(
        default_factory=dict,
        description="Canonical answer data only. Retrieval-only artifacts must not appear here.",
    )


class WhatEvidence(BaseModel):
    """Structured evidence retained for reasoning after normalization."""

    model_config = ConfigDict(extra="forbid")

    structured: dict[str, Any] = Field(
        default_factory=dict,
        description="Compact structured evidence that still helps the response agent reason safely.",
    )
    fallback: dict[str, Any] = Field(
        default_factory=dict,
        description="Fallback evidence such as crawled/reference data used only when structured evidence is empty.",
    )
    media: dict[str, Any] = Field(
        default_factory=dict,
        description="Media availability and explicit attachment metadata when relevant to the answer.",
    )


class WhatPagination(BaseModel):
    """Pagination metadata for list/count-like WHAT results."""

    model_config = ConfigDict(extra="forbid")

    total: int = Field(default=0, ge=0, description="Total number of matching items in the full result set.")
    display_limit: int = Field(default=0, ge=0, description="Maximum number of items intended for one answer turn.")
    next_start_index: int = Field(default=0, ge=0, description="Cursor index for the next continue-listing turn.")

    @model_validator(mode="after")
    def validate_pagination_consistency(self) -> "WhatPagination":
        if self.display_limit == 0 and self.next_start_index > 0:
            raise ValueError("next_start_index cannot be set when display_limit is 0.")
        if self.total == 0 and self.next_start_index > 0:
            raise ValueError("next_start_index cannot be greater than 0 when total is 0.")
        return self


class WhatMeta(BaseModel):
    """Metadata describing how the final canonical answer was obtained."""

    model_config = ConfigDict(extra="forbid")

    source: WhatSource = Field(default=WhatSource.GRAPH, description="Dominant source of the final canonical answer.")
    used_fallback: bool = Field(
        default=False,
        description="Whether the final answer relied on fallback retrieval instead of the direct structured branch.",
    )

    @model_validator(mode="after")
    def align_fallback_flag(self) -> "WhatMeta":
        if self.source == WhatSource.CRAWLED and not self.used_fallback:
            self.used_fallback = True
        return self


class WhatQueryResult(BaseModel):
    """Canonical chatbot-facing contract for all WHAT query-plan outputs."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    question_family: Literal["what"] = Field(
        default=WhatQuestionFamily.WHAT.value,
        description="Top-level question family. Always 'what' for this contract.",
    )
    intent: WhatIntent = Field(description="Normalized WHAT intent routed from the query plan.")
    status: WhatStatus = Field(
        default=WhatStatus.EMPTY,
        description="Whether the final normalized result is direct, fallback-based, or empty.",
    )
    formatted_answer: str | None = Field(
        default=None,
        description="High-priority preformatted answer, when a query plan can produce one safely.",
    )
    entities: WhatEntities = Field(
        default_factory=WhatEntities,
        description="Resolved entities that remain useful to the chatbot after normalization.",
    )
    answer: WhatAnswer = Field(description="Canonical answer payload.")
    evidence: WhatEvidence = Field(
        default_factory=WhatEvidence,
        description="Compact evidence retained for downstream reasoning and grounded answer generation.",
    )
    pagination: WhatPagination = Field(
        default_factory=WhatPagination,
        description="Pagination metadata. Zeroed for non-collection answers.",
    )
    meta: WhatMeta = Field(
        default_factory=WhatMeta,
        description="Source metadata describing where the normalized answer came from.",
    )

    @model_validator(mode="after")
    def validate_intent_answer_pair(self) -> "WhatQueryResult":
        expected_kind = {
            WhatIntent.ATTRIBUTES: WhatAnswerKind.ATTRIBUTES,
            WhatIntent.COMPARE: WhatAnswerKind.COMPARISON,
            WhatIntent.RELATION: WhatAnswerKind.RELATION,
            WhatIntent.LIST: WhatAnswerKind.COLLECTION,
            WhatIntent.COUNT: WhatAnswerKind.COLLECTION,
            WhatIntent.CONSTRAINT_LIST: WhatAnswerKind.COLLECTION,
            WhatIntent.UNKNOWN: self.answer.kind,
        }[self.intent]
        if self.answer.kind != expected_kind:
            raise ValueError(f"Intent '{self.intent}' must use answer.kind '{expected_kind}'.")

        if self.status == WhatStatus.EMPTY and self.answer.data:
            raise ValueError("Empty WHAT results must not contain answer data.")

        if self.status == WhatStatus.FALLBACK and not self.meta.used_fallback:
            self.meta.used_fallback = True

        if self.status == WhatStatus.OK and self.meta.source == WhatSource.CRAWLED:
            raise ValueError("Status 'ok' cannot use crawled source as the primary source.")

        return self
