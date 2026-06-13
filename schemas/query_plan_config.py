from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

# =============================================================================
# ENUMS
# =============================================================================


class EntityType(StrEnum):

    MAJOR = "Major"
    SPECIALIZATION = "Specialization"
    FACULTY = "Faculty"
    ACADEMIC_PROGRAM = "AcademicProgram"
    COURSE = "Course"
    CAMPUS = "Campus"
    UNIVERSITY = "University"
    PERSON = "Person"


class QuestionType(StrEnum):

    WHAT = "WHAT"
    WHY = "WHY"
    HOW = "HOW"
    WHICH = "WHICH"
    MIXED = "MIXED"


class IntentType(StrEnum):

    LOOKUP = "lookup"
    LIST = "list"
    CAREER_LIST = "career_list"
    COUNT = "count"
    EXPLAIN = "explain"
    PROCEDURE = "procedure"
    COMPARE = "compare"
    UNKNOWN = "unknown"


class PrimaryTopic(StrEnum):

    UNIVERSITY = "university"
    FACULTY = "faculty"
    CAREER = "career"
    MAJOR = "major"
    PROGRAM = "program"
    COURSE = "course"
    ADMISSION = "admission"
    FEE = "fee"
    POLICY = "policy"
    STUDENT_LIFE = "student_life"
    CAMPUS = "campus"
    PEOPLE = "people"


class AmbiguityLevel(StrEnum):

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# =============================================================================
# =============================================================================


class ExtractedEntity(BaseModel):

    label: str
    text: str


class TimeInfo(BaseModel):

    mode: Literal["year", "range", "unknown"] = "unknown"
    year: int | None = 0
    from_year: int | None = 0
    to_year: int | None = 0
    raw: str | None = None


class TimeCompare(BaseModel):

    mode: Literal["year", "range", "unknown"] = "unknown"
    from_year: int | None = 0
    to_year: int | None = 0
    from_raw: str | None = None
    to_raw: str | None = None


class ExtractedIntent(BaseModel):

    question_type: QuestionType = QuestionType.WHAT
    intent: IntentType = IntentType.UNKNOWN
    primary_topic: PrimaryTopic = PrimaryTopic.UNIVERSITY

    # Entities
    primary_entities: list[ExtractedEntity] = Field(default_factory=list)
    context_entities: list[ExtractedEntity] = Field(default_factory=list)

    # Compare mode
    compare_mode: bool = False
    compare_targets: list[ExtractedEntity] = Field(default_factory=list)

    # Additional info
    keywords: list[str] = Field(default_factory=list)
    subtopics: list[str] = Field(default_factory=list)

    # Ambiguity
    ambiguity_level: AmbiguityLevel = AmbiguityLevel.LOW
    needs_disambiguation: bool = False

    # Time
    time: TimeInfo | None = None
    time_compare: TimeCompare | None = None

    count_enumerate_targets: list[str] = Field(default_factory=list)
    count_has_condition: bool = False


# =============================================================================


class ResolvedEntity(BaseModel):

    entity_type: str
    entity_id: str
    entity_name: str
    similarity_score: float = Field(ge=0, le=2)
    description: str | None = None


# =============================================================================
# QUERY FLOW MODELS
# =============================================================================


class FlowConfig(BaseModel):

    flow_file: str
    entities_to_resolve: list[ExtractedEntity]
    query_mode: Literal["single", "compare", "list"] = "single"
    subtopics: list[str] = Field(default_factory=list)
    time_filter: TimeInfo | None = None


class IntentClassification(BaseModel):

    # extracted_intent: ExtractedIntent
    flow_config: FlowConfig
    # confidence: float = Field(ge=0, le=1)


class TraversalStepResult(BaseModel):

    step_name: str
    data: list[dict[str, Any]]
    cypher_query: str


class QueryFlowResult(BaseModel):

    resolved_entity: ResolvedEntity
    traversal_results: dict[str, TraversalStepResult]
    raw_context: str


class MultiEntityQueryResult(BaseModel):

    query_mode: Literal["single", "compare", "list", "multi"]
    results: dict[str, QueryFlowResult]
    combined_context: str


# =============================================================================
# API MODELS
# =============================================================================


class ChatRequest(BaseModel):

    question: str = Field(..., min_length=1, max_length=1000)
    user_profile: dict[str, Any] | None = None


class ArgumentMap(BaseModel):

    claim: str
    causes: list[dict[str, Any]]
    capabilities: list[dict[str, Any]]
    comparison_frame: dict[str, Any] | None = None
    direction: list[dict[str, Any]]


class ChatResponse(BaseModel):

    answer: str
    resolved_entities: list[ResolvedEntity] | None = None
    rule_matched: str | None = None
    argument_map: ArgumentMap | None = None
    debug_info: dict[str, Any] | None = None
