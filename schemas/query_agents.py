from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

# Typed sub-models for entity references
# Using concrete models instead of Dict[str, str] so the JSON Schema
# sent to Gemini enforces non-empty fields (min_length=1).
# when no entities are found.

PrimaryTopicLiteral = Literal[
    "people",
    "faculty",
    "career",
    "campus",
    "fee",
    "admission",
    "program",
    "course",
    "major",
    "policy",
    "support",
    "student_life",
    "university",
]


class EntityRef(BaseModel):
    """A reference to a named entity with label (type) and text (name)."""

    label: str = Field(
        default="",
        min_length=1,
        description="Entity type, representing the node label in the knowledge graph. (e.g. 'Major', 'University', 'Person')",
        json_schema_extra={
            "example": "Major",
        },
    )
    text: str = Field(
        default="",
        min_length=1,
        description="Entity name or exact surface phrase extracted from the user question. Preserve the user-mentioned text unless expanding an explicit abbreviation or resolving an omitted reference from history; do not replace it with a merely related/canonical sibling entity.",
        json_schema_extra={
            "example": "Ngành Công nghệ thông tin",
        },
    )


class PotentialEntityRef(BaseModel):
    """A potential entity inferred from the question (WHY/HOW only)."""

    type: PrimaryTopicLiteral = Field(
        default="major",
        description="Entity type mapped to a primary_topic value.",
        json_schema_extra={
            "example": "major",
        },
    )
    label: str = Field(
        default="",
        min_length=1,
        description="Inferred entity name based on the context.",
        json_schema_extra={
            "example": "Trí tuệ nhân tạo",
        },
    )


class QueryAgentOutputSchema(BaseModel):
    is_query: bool = Field(description="Whether this is a knowledge query")
    query_type: Literal["definition", "attribute", "enumeration", "comparison", "not_query"] = Field(
        description="Type of query"
    )
    target_entities: list[str] | None = Field(
        description="Target entities to query",
    )
    related_entities_type: list[str] | None = Field(
        description="Related entity types",
    )
    related_entities: list[str] | None = Field(
        description="Related entities name",
    )
    target_attribute: list[str] | None = Field(
        description="Requested attributes",
    )
    related_entities_attribute: list[str] | None = Field(
        description="Related entities attributes",
    )
    original_query: str | None = Field(
        description="Original user question",
    )
    question_intent: str | None = Field(
        description="Intent of the question",
    )
    effective_from: str | None = Field(
        description="Effective from date",
    )
    is_required_media: bool = Field(
        description="Whether user wants to see media",
    )
    effective_to: str | None = Field(
        description="Effective to date",
    )

    # NEW SCHEMA FIELDS (For WHY, HOW, WHICH, MIXED)
    question_type: Literal["WHAT", "WHY", "HOW", "WHICH", "MIXED", "not_query"] = Field(
        description="Classified question type"
    )
    intent: Literal["lookup", "list", "career_list", "count", "explain", "procedure", "compare", "unknown"] | None = Field(
        description="Intent of the question (New Schema)"
    )
    primary_topic: PrimaryTopicLiteral | None = Field(
        description="Primary topic of the question",
    )
    primary_entities: list[dict[str, str]] | None = Field(
        description="Main entities extracted",
    )
    context_entities: list[dict[str, str]] | None = Field(
        description="Context entities (e.g. University)",
    )
    compare_mode: bool | None = Field(
        description="Whether query is a comparison. Applies to normal entity comparison, relation comparison across multiple objects, and relation comparison across time.",
    )
    compare_targets: list[dict[str, str]] | None = Field(
        description="Compared entities after the first side. The first compared side goes in primary_entities; all remaining compared sides go here in order.",
    )
    keywords: list[str] | None = Field(
        description="Keywords for search",
    )
    subtopics: list[str] | None = Field(
        description="Subtopics identified",
    )
    ambiguity_level: Literal["low", "medium", "high"] | None = Field(
        description="Ambiguity level of the query",
    )
    needs_disambiguation: bool | None = Field(
        description="If disambiguation is needed",
    )
    time: dict[str, Any] | None = Field(
        description='Time context. If the user does not explicitly mention time, default this to {"from_year": null, "to_year": current_year} instead of null. Month-granular queries may also use from_month/to_month.',
    )
    time_compare: dict[str, Any] | None = Field(
        description="Time comparison context for two time periods. MUST be null if no comparison time is explicitly mentioned. Month-granular comparisons may also use from_month/to_month.",
    )
    potential_entities: list[dict[str, str]] | None = Field(
        description="Inferred potential entities from question content when primary_entities is empty. Each item has 'type' (must be a valid primary_topic value) and 'label' (entity name). E.g. [{'type': 'Major', 'label': 'AI'}]",
    )
    count_enumerate_targets: list[str] = Field(
        default_factory=list,
        description="Target entity types that are being listed or counted (MUST be exact Node Labels, e.g., 'Major', 'TeachingAndLearningMethod')",
    )
    count_has_condition: bool = Field(
        description="Whether the count query has filtering conditions. true for conditional counts (role/degree/status), false for simple counts. false when intent != 'count'."
    )


class RelationSchema(BaseModel):
    """Schema for extracted relation"""

    source: str = Field(
        description="Source entity type in the relationship",
    )
    relationship: str = Field(
        description="Relationship type name (e.g., APPLIES_TO, TRAINS, WORKS_IN)",
    )
    target: str = Field(
        description="Target entity type in the relationship",
    )


class TimeContext(BaseModel):
    """Temporal context extracted from the query to filter data by time."""

    from_year: int | None = Field(
        default=None,
        description="Start year for an explicit single-year or start-bound query. Leave null when the user does not explicitly mention time so the default current-year boundary can be represented by to_year.",
        json_schema_extra={
            "example": 2026,
        },
    )
    from_month: int | None = Field(
        default=None,
        ge=1,
        le=12,
        description="Start month for a month-granular query. Use together with from_year when the user mentions a specific month or relative month.",
        json_schema_extra={
            "example": 6,
        },
    )
    to_year: int | None = Field(
        default=None,
        description="End year for the default current-year boundary when no time is mentioned, or for an explicit end boundary. Keep null for explicit single-year/start-bound queries.",
        json_schema_extra={
            "example": 2026,
        },
    )
    to_month: int | None = Field(
        default=None,
        ge=1,
        le=12,
        description="End month for a month-granular query. Use together with to_year when the user mentions a specific month or relative month.",
        json_schema_extra={
            "example": 6,
        },
    )


class TimeCompareContext(BaseModel):
    """Temporal context used when comparing data across two different time periods."""

    mode: Literal["year", "range", "unknown"] = Field(
        default="unknown",
        description="Mode of the time comparison context.",
        json_schema_extra={
            "example": "year",
        },
    )
    from_raw: str | None = Field(
        default=None,
        description="Raw string representing the base/first time period. Leave null when not explicitly provided.",
        json_schema_extra={
            "example": "năm ngoái",
        },
    )
    to_raw: str | None = Field(
        default=None,
        description="Raw string representing the target/second time period. Leave null when not explicitly provided.",
        json_schema_extra={
            "example": "năm nay",
        },
    )
    from_year: int | None = Field(
        default=None, description="Resolved start year for comparison.", json_schema_extra={"example": 2023}
    )
    from_month: int | None = Field(
        default=None,
        ge=1,
        le=12,
        description="Resolved start month for comparison when month granularity is needed.",
        json_schema_extra={"example": 1},
    )
    to_year: int | None = Field(
        default=None, description="Resolved end year for comparison.", json_schema_extra={"example": 2024}
    )
    to_month: int | None = Field(
        default=None,
        ge=1,
        le=12,
        description="Resolved end month for comparison when month granularity is needed.",
        json_schema_extra={"example": 12},
    )


class QueryAgentOutputSchemaV2(BaseModel):
    """
    Schema for parsing user queries into structured JSON for a knowledge graph and vector search backend.
    Maps user questions to specific intents, extracts entities, time contexts, and search attributes.
    """

    reasoning_explain: str = Field(
        default="",
        description="Step-by-step reasoning explaining how the query was parsed, why specific intents and entities were chosen, and how ambiguities were resolved.",
        json_schema_extra={
            "example": "Người dùng hỏi 'Điểm chuẩn ngành CNTT'. Vì có 'điểm chuẩn' (AdmissionMethod) và 'ngành CNTT' (Major) -> 2 loại entity khác nhau -> intent='relation'.",
        },
    )

    question_type: Literal["WHAT", "WHY", "HOW", "not_query"] = Field(
        default="WHAT",
        description=(
            "Broad classification of the user's question. "
            "Questions about THPT schools, other universities, or casual greetings are 'not_query'."
        ),
        json_schema_extra={
            "example": "WHAT",
        },
    )

    intent: Literal[
        "definition",
        "attributes",
        "list",
        "career_list",
        "constraint-list",
        "relation",
        "count",
        "explain",
        "procedure",
        "unknown",
    ] = Field(
        default="unknown",
        description=(
            "Specific intent mapping to the question_type. "
            "For WHAT: 'attributes' (single entity properties), 'definition' (meaning/concept), "
            "'relation' (traversal between 2+ entity types), 'list' (pure enumeration), "
            "'career_list' (career/preference-based major recommendation), "
            "'count' (counting entities), 'constraint-list' (filtered enumeration). "
            "Role/title/group membership lookup such as 'trưởng khoa', 'hiệu trưởng', "
            "'phó hiệu trưởng', 'Hội đồng trường', or 'Ban giám hiệu' MUST be 'relation', "
            "not 'list', because the system is traversing people-role/group relation inside an organizational scope. "
            "For WHY: 'explain'. "
            "For HOW: 'explain', 'procedure'. "
            "If none matches or unclear: 'unknown'."
        ),
        json_schema_extra={
            "example": "relation",
        },
    )

    is_query: bool = Field(
        default=True,
        description="True for knowledge queries. False for greetings, personal info, or out-of-scope topics.",
        json_schema_extra={
            "example": True,
        },
    )

    primary_topic: PrimaryTopicLiteral = Field(
        default="university",
        description=(
            "Main subject category. E.g.: people, faculty, career, fee, admission, program, course, "
            "major, policy, support, student_life, university."
        ),
        json_schema_extra={
            "example": "admission",
        },
    )

    primary_entities: list[EntityRef] = Field(
        default_factory=list,
        description=(
            "The core entities being asked about. Return [] if no entities. "
            "For attributes/definition intents, this MUST contain the concrete entity whose "
            "attribute, fact, description, status, schedule, contact detail, or other property "
            "is requested, regardless of label. "
            "For relation intents, this should be the candidate/property entity being checked against the context scope. "
            "For role/title/group membership lookup queries, store the role/title/group phrase itself as Person here "
            "(e.g. {'label': 'Person', 'text': 'trưởng khoa'} or {'label': 'Person', 'text': 'Hội đồng trường'}). "
            "Do NOT place the exact same named entity in both primary_entities and context_entities."
        ),
        json_schema_extra={
            "example": [
                {
                    "label": "AdmissionMethod",
                    "text": "Điểm chuẩn",
                }
            ],
        },
    )

    context_entities: list[EntityRef] = Field(
        default_factory=list,
        description="Entities providing scope/context. Defaults to University if empty and is_query is true (unless a narrower scope like Major/Faculty is present).",
        json_schema_extra={
            "example": [
                {
                    "label": "University",
                    "text": "Trường Đại học Gia Định",
                }
            ],
        },
    )

    count_enumerate_targets: list[str] | None = Field(
        default_factory=list,
        description=(
            "Exact node labels user wants to list or count. For single-level enumeration, return one label "
            "(e.g. 'Major'). For hierarchical breakdown questions, return the full ordered chain of enumerated "
            "labels from outer set to inner/detail set (e.g. ['Faculty', 'Major'] or ['Major', 'Course']). "
            "Leave this empty for role/title/group membership relation lookups such as 'ai là hiệu trưởng', "
            "'các khoa trên có những trưởng khoa nào', or 'Hội đồng trường gồm những ai'. "
            "Only for 'list', 'constraint-list', or 'count' intents."
        ),
        json_schema_extra={
            "example": ["Major"],
        },
    )

    count_has_condition: bool = Field(
        default=False,
        description="True when intent is 'constraint-list', or when intent is 'count' and there are explicit filtering conditions (e.g., 'bao nhiêu Tiến sĩ'). False for unconditional counts/lists.",
        json_schema_extra={
            "example": False,
        },
    )

    compare_mode: bool = Field(
        default=False,
        description="True when the user explicitly compares multiple sides. This includes normal entity comparison, relation comparison across multiple objects, and time-based relation comparison where the same relation is compared across two explicit years.",
        json_schema_extra={
            "example": False,
        },
    )

    compare_targets: list[EntityRef] = Field(
        default_factory=list,
        description=(
            "The compared entities after the first side. Populated only when compare_mode is true. "
            "The FIRST compared side goes in primary_entities, and all remaining sides go here in user order. "
            "primary_entities and compare_targets MUST NOT overlap."
        ),
        json_schema_extra={
            "example": [
                {
                    "label": "Major",
                    "text": "Kế toán",
                },
            ],
        },
    )

    keywords: list[str] = Field(
        default_factory=list,
        description=(
            "Short phrases (1-4 words) extracting the main subjects and constraints. "
            "Include the numeric THRESHOLD part of conditions as a SEPARATE keyword "
            "(e.g., 'trên 16', 'lớn hơn 600 điểm'). "
            "The ATTRIBUTE NAME (e.g., 'điểm chuẩn', 'học phí') MUST STILL appear in "
            "primary_entities — do NOT remove it from primary_entities just because "
            "it also appears here. Exclude the entity type word being counted."
        ),
        json_schema_extra={
            "example": ["điểm chuẩn", "năm 2024", "trên 16"],
        },
    )

    subtopics: list[str] = Field(
        default_factory=list,
        description="Specific thematic aspects to focus on (career, skills, curriculum, outcomes, ecosystem, partners, facilities, services, campus, cost_fee, procedure, cutoff, quota, combination, method, priority, scholarship, market_trend, ethics, support, academic_policy, tuition, payment, course_registration, student_document, graduation, major_transfer, internship, event, activity, club, service, department_support, discipline, reward, admission, media). For current-student support questions outside admissions, include 'support' plus the most specific support tag; examples include academic warning, policy scholarship, course registration, student documents, graduation, internship, events, clubs, services, departments, discipline, or rewards. Do not use this field to store omitted enumerated entity types from hierarchical list/count questions.",
        json_schema_extra={
            "example": ["cost_fee"],
        },
    )

    ambiguity_level: Literal["low", "medium", "high"] = Field(
        default="low",
        description="Estimated query clarity: 'low' for full names, 'medium' for abbreviations, 'high' for missing scope.",
        json_schema_extra={
            "example": "low",
        },
    )

    needs_disambiguation: bool = Field(
        default=False,
        description="True if agent needs user clarification (e.g. if ambiguity_level='high').",
        json_schema_extra={
            "example": False,
        },
    )

    time: TimeContext | None = Field(
        default=None,
        description='Temporal context for filtering (e.g. admission year). If the user does not mention time, default this to {"from_year": null, "to_year": current_year} instead of null. For explicit single-year/start-bound queries, set from_year and keep to_year null. For explicit end boundaries, set to_year.',
        json_schema_extra={
            "example": {
                "from_year": None,
                "to_year": 2026,
            }
        },
    )

    time_compare: TimeCompareContext | None = Field(
        default=None,
        description="Temporal context for comparing two specific times. MUST be null if the query has no explicit time comparison, including object-vs-object relation compare cases. When this field is non-null in WHAT queries, the parser should prefer intent='relation' for same-relation-across-time comparison cases.",
    )

    potential_entities: list[PotentialEntityRef] = Field(
        default_factory=list,
        description="Inferred entities when primary_entities is empty. Typically for WHY/HOW questions where an entity is implied.",
        json_schema_extra={
            "example": [
                {
                    "type": "major",
                    "label": "Ngành Trí tuệ nhân tạo",
                }
            ],
        },
    )

    keyword_attributes: list[str] = Field(
        default_factory=list,
        description="Extract 2-3 verbatim attribute phrases from the question describing WHAT information is requested (e.g. fee, description), then add synonyms/translations (Vietnamese+English). NEVER include entity types or names.",
        json_schema_extra={
            "example": ["học phí", "tuition fee", "chi phí"],
        },
    )

    original_query: str = Field(
        default="",
        description="The rewritten, clear, standalone Vietnamese sentence representing the parsed sub-query.",
        json_schema_extra={
            "example": "Mức học phí của ngành Công nghệ thông tin là bao nhiêu?",
        },
    )

    @model_validator(mode="after")
    def _normalize_attribute_subject_placement(self):
        """Ensure attribute/definition subjects are also present in primary_entities."""
        if self.intent not in {"attributes", "definition"}:
            return self

        if not self.primary_entities and self.context_entities:
            self.primary_entities = list(self.context_entities)
        return self


class QueryAgentOutputList(BaseModel):
    """
    Gemini-friendly wrapper with object root.
    Accepts both {"items": [...]} and bare [...] from LLM.
    """

    @model_validator(mode="before")
    @classmethod
    def _coerce_list_to_items(cls, v: Any) -> Any:
        """If LLM returns a bare array instead of {"items": [...]}, wrap it."""
        if isinstance(v, list):
            return {"items": v}
        return v

    items: list[QueryAgentOutputSchemaV2] = Field(
        default_factory=list,
        description="List of parsed sub-queries. If the user asks a multiple/mixed questions (e.g. WHAT and WHY together), split them into separate items in this array.",
        json_schema_extra={
            "example": [
                {
                    "question_type": "WHAT",
                    "intent": "attributes",
                    "original_query": "Tỷ lệ tốt nghiệp là bao nhiêu?",
                }
            ],
        },
    )
