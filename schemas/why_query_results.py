from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class WhyQuestionFamily(StrEnum):
    """Supported question family for canonical WHY query results."""

    WHY = "why"


class WhyIntent(StrEnum):
    """Canonical WHY intent values exposed to downstream consumers."""

    ADMISSION = "admission"
    COURSE = "course"
    EXPLAIN_MAJOR = "explain_major"
    STUDENT_LIFE = "student_life"
    FACULTY = "faculty"
    FEE = "fee"
    KEYWORD_MAJOR = "keyword_major"
    POLICY = "policy"
    PROGRAM = "program"
    UNIVERSITY = "university"
    UNKNOWN = "unknown"


class WhyStatus(StrEnum):
    """Execution status of the final canonical result."""

    OK = "ok"
    FALLBACK = "fallback"
    EMPTY = "empty"


class WhyAnswerKind(StrEnum):
    """High-level answer shape used by downstream answer prompting."""

    ARGUMENT = "argument"


class WhySource(StrEnum):
    """Origin of the answer evidence after normalization."""

    GRAPH = "graph"
    HYBRID = "hybrid"
    CRAWLED = "crawled"


class WhyEntityRef(BaseModel):
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


class WhyEntities(BaseModel):
    """Resolved entities grouped by their conversational role."""

    model_config = ConfigDict(extra="forbid")

    primary: list[WhyEntityRef] = Field(default_factory=list, description="Resolved primary entities asked about.")
    context: list[WhyEntityRef] = Field(default_factory=list, description="Resolved context/scope entities.")


class WhyAnswer(BaseModel):
    """Canonical answer payload consumed by the response agent."""

    model_config = ConfigDict(extra="forbid")

    kind: WhyAnswerKind = Field(description="Stable answer kind for downstream routing and prompting.")
    data: dict[str, Any] = Field(
        default_factory=dict,
        description="Canonical answer data only. Retrieval-only artifacts must not appear here.",
    )


class WhyEvidence(BaseModel):
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


class WhyMeta(BaseModel):
    """Metadata describing how the final canonical answer was obtained."""

    model_config = ConfigDict(extra="forbid")

    source: WhySource = Field(default=WhySource.GRAPH, description="Dominant source of the final canonical answer.")
    used_fallback: bool = Field(
        default=False,
        description="Whether the final answer relied on fallback retrieval instead of the direct structured branch.",
    )

    @model_validator(mode="after")
    def align_fallback_flag(self) -> "WhyMeta":
        if self.source == WhySource.CRAWLED and not self.used_fallback:
            self.used_fallback = True
        return self


class WhyQueryResult(BaseModel):
    """Canonical chatbot-facing contract for all WHY query-plan outputs."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    question_family: Literal["why"] = Field(
        default=WhyQuestionFamily.WHY.value,
        description="Top-level question family. Always 'why' for this contract.",
    )
    intent: WhyIntent = Field(description="Normalized WHY intent routed from the query plan.")
    status: WhyStatus = Field(
        default=WhyStatus.EMPTY,
        description="Whether the final normalized result is direct, fallback-based, or empty.",
    )
    entities: WhyEntities = Field(
        default_factory=WhyEntities,
        description="Resolved entities that remain useful to the chatbot after normalization.",
    )
    answer: WhyAnswer = Field(description="Canonical answer payload.")
    evidence: WhyEvidence = Field(
        default_factory=WhyEvidence,
        description="Compact evidence retained for downstream reasoning and grounded answer generation.",
    )
    meta: WhyMeta = Field(
        default_factory=WhyMeta,
        description="Source metadata describing where the normalized answer came from.",
    )

    @model_validator(mode="after")
    def validate_intent_answer_pair(self) -> "WhyQueryResult":
        expected_kind = {
            WhyIntent.ADMISSION: WhyAnswerKind.ARGUMENT,
            WhyIntent.COURSE: WhyAnswerKind.ARGUMENT,
            WhyIntent.EXPLAIN_MAJOR: WhyAnswerKind.ARGUMENT,
            WhyIntent.STUDENT_LIFE: WhyAnswerKind.ARGUMENT,
            WhyIntent.FACULTY: WhyAnswerKind.ARGUMENT,
            WhyIntent.FEE: WhyAnswerKind.ARGUMENT,
            WhyIntent.KEYWORD_MAJOR: WhyAnswerKind.ARGUMENT,
            WhyIntent.POLICY: WhyAnswerKind.ARGUMENT,
            WhyIntent.PROGRAM: WhyAnswerKind.ARGUMENT,
            WhyIntent.UNIVERSITY: WhyAnswerKind.ARGUMENT,
            WhyIntent.UNKNOWN: self.answer.kind,
        }[self.intent]
        if self.answer.kind != expected_kind:
            raise ValueError(f"Intent '{self.intent}' must use answer.kind '{expected_kind}'.")

        if self.status == WhyStatus.EMPTY and self.answer.data:
            raise ValueError("Empty WHY results must not contain answer data.")

        if self.status == WhyStatus.FALLBACK and not self.meta.used_fallback:
            self.meta.used_fallback = True

        if self.status == WhyStatus.OK and self.meta.source == WhySource.CRAWLED:
            raise ValueError("Status 'ok' cannot use crawled source as the primary source.")

        return self
