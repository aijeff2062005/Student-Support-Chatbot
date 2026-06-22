from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class HowQuestionFamily(StrEnum):
    """Supported question family for canonical HOW query results."""

    HOW = "how"


class HowIntent(StrEnum):
    """Canonical HOW intent values exposed to downstream consumers."""

    ADMISSION = "admission"
    COURSE = "course"
    FEE = "fee"
    DOCUMENT = "document"
    POLICY = "policy"
    ACTIVITY = "activity"
    STUDENT_LIFE = "student_life"
    FACILITIES = "facilities"
    CAMPUS_CONTACT = "campus_contact"
    SKILL_TRAINING = "skill_training"
    SKILL_TO_MAJOR = "skill_to_major"
    MAJOR_GUIDANCE = "major_guidance"
    MAJOR_TO_CAREER_PATH = "major_to_career_path"
    CAREER_POSITION = "career_position"
    UNKNOWN = "unknown"


class HowStatus(StrEnum):
    """Execution status of the final canonical result."""

    OK = "ok"
    FALLBACK = "fallback"
    EMPTY = "empty"


class HowAnswerKind(StrEnum):
    """High-level answer shape used by downstream answer prompting."""

    PROCEDURE = "procedure"
    ROADMAP = "roadmap"
    CONTACT = "contact"
    ELIGIBILITY = "eligibility"
    GUIDANCE = "guidance"


class HowSource(StrEnum):
    """Origin of the answer evidence after normalization."""

    GRAPH = "graph"
    HYBRID = "hybrid"
    CRAWLED = "crawled"


class HowEntityRef(BaseModel):
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


class HowEntities(BaseModel):
    """Resolved entities grouped by their conversational role."""

    model_config = ConfigDict(extra="forbid")

    primary: list[HowEntityRef] = Field(default_factory=list, description="Resolved primary entities asked about.")
    context: list[HowEntityRef] = Field(default_factory=list, description="Resolved context/scope entities.")


class HowAnswer(BaseModel):
    """Canonical answer payload consumed by the response agent."""

    model_config = ConfigDict(extra="forbid")

    kind: HowAnswerKind = Field(description="Stable answer kind for downstream routing and prompting.")
    data: dict[str, Any] = Field(
        default_factory=dict,
        description="Canonical answer data only. Retrieval-only artifacts must not appear here.",
    )


class HowEvidence(BaseModel):
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


class HowMeta(BaseModel):
    """Metadata describing how the final canonical answer was obtained."""

    model_config = ConfigDict(extra="forbid")

    source: HowSource = Field(default=HowSource.GRAPH, description="Dominant source of the final canonical answer.")
    used_fallback: bool = Field(
        default=False,
        description="Whether the final answer relied on fallback retrieval instead of the direct structured branch.",
    )

    @model_validator(mode="after")
    def align_fallback_flag(self) -> "HowMeta":
        if self.source == HowSource.CRAWLED and not self.used_fallback:
            self.used_fallback = True
        return self


class HowQueryResult(BaseModel):
    """Canonical chatbot-facing contract for all HOW query-plan outputs."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    question_family: Literal["how"] = Field(
        default=HowQuestionFamily.HOW.value,
        description="Top-level question family. Always 'how' for this contract.",
    )
    intent: HowIntent = Field(description="Normalized HOW intent routed from the query plan.")
    status: HowStatus = Field(
        default=HowStatus.EMPTY,
        description="Whether the final normalized result is direct, fallback-based, or empty.",
    )
    entities: HowEntities = Field(
        default_factory=HowEntities,
        description="Resolved entities that remain useful to the chatbot after normalization.",
    )
    answer: HowAnswer = Field(description="Canonical answer payload.")
    evidence: HowEvidence = Field(
        default_factory=HowEvidence,
        description="Compact evidence retained for downstream reasoning and grounded answer generation.",
    )
    meta: HowMeta = Field(
        default_factory=HowMeta,
        description="Source metadata describing where the normalized answer came from.",
    )

    @model_validator(mode="after")
    def validate_intent_answer_pair(self) -> "HowQueryResult":
        expected_kind = {
            HowIntent.ADMISSION: HowAnswerKind.PROCEDURE,
            HowIntent.COURSE: HowAnswerKind.ROADMAP,
            HowIntent.FEE: HowAnswerKind.PROCEDURE,
            HowIntent.DOCUMENT: HowAnswerKind.PROCEDURE,
            HowIntent.POLICY: HowAnswerKind.PROCEDURE,
            HowIntent.ACTIVITY: HowAnswerKind.PROCEDURE,
            HowIntent.STUDENT_LIFE: HowAnswerKind.PROCEDURE,
            HowIntent.FACILITIES: HowAnswerKind.PROCEDURE,
            HowIntent.CAMPUS_CONTACT: HowAnswerKind.CONTACT,
            HowIntent.SKILL_TRAINING: HowAnswerKind.ROADMAP,
            HowIntent.SKILL_TO_MAJOR: HowAnswerKind.ELIGIBILITY,
            HowIntent.MAJOR_GUIDANCE: HowAnswerKind.GUIDANCE,
            HowIntent.MAJOR_TO_CAREER_PATH: HowAnswerKind.ROADMAP,
            HowIntent.CAREER_POSITION: HowAnswerKind.ROADMAP,
            HowIntent.UNKNOWN: self.answer.kind,
        }[self.intent]
        if self.answer.kind != expected_kind:
            raise ValueError(f"Intent '{self.intent}' must use answer.kind '{expected_kind}'.")

        if self.status == HowStatus.EMPTY and self.answer.data:
            raise ValueError("Empty HOW results must not contain answer data.")

        if self.status == HowStatus.FALLBACK and not self.meta.used_fallback:
            self.meta.used_fallback = True

        if self.status == HowStatus.OK and self.meta.source == HowSource.CRAWLED:
            raise ValueError("Status 'ok' cannot use crawled source as the primary source.")

        return self
