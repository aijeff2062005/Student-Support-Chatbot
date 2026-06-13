from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class UserRole(StrEnum):
    student = "student"
    parent = "parent"
    grand_parent = "grand_parent"


class TopicType(StrEnum):
    SCHOLARSHIP = "scholarship"
    ADMISSION = "admission"
    PROGRAM = "program"
    CAREER = "career"


class PersonalInfo(BaseModel):
    """User's personal information."""

    full_name: str | None = Field(default=None, description="User's full name")
    phone: str | None = Field(default=None, description="User's phone number")
    email: str | None = Field(default=None, description="User's email address")
    gender: str | None = Field(default=None, description="User's gender (male/female), passively extracted")
    date_of_birth: str | None = Field(
        default=None, description="User's date of birth in yyyy-mm-dd format, passively extracted"
    )
    province_location: str | None = Field(
        default=None, description="User's current living province/city, passively extracted"
    )
    old_province_location: str | None = Field(
        default=None,
        description="Raw current living province/city value as provided by the user before province conversion",
    )
    ward_location: str | None = Field(
        default=None, description="User's current ward/commune and district, passively extracted. Free-text."
    )
    high_school: str | None = Field(default=None, description="User's high school name")
    high_school_province: str | None = Field(default=None, description="User's high school province")
    old_high_school_province: str | None = Field(
        default=None,
        description="Raw high school province value as provided by the user before province conversion",
    )
    high_school_address: str | None = Field(default=None, description="User's high school address")
    admission_methods: list[dict[str, str]] = Field(
        default_factory=list,
        description="Admission methods the user is interested in, format: [{method_code: method_name}]",
    )
    address_detail: str | None = Field(
        default=None,
        description="User's detailed sub-ward address: house number, street, alley, apartment, residential group, neighborhood block, etc. Free-text, passively extracted.",
    )


MajorInterestEntityType = Literal["Major", "Specialization"]
MajorInterestPolarity = Literal["positive", "negative", "neutral"]
MajorInterestSignalType = Literal[
    "explicit_choice",
    "strong_commitment",
    "preference",
    "comparative_preference",
    "soft_interest",
    "exploration",
    "uncertainty",
    "negative_preference",
    "replacement_choice",
    "hard_drop",
]


class MajorInterestEvent(BaseModel):
    entity_code: str = Field(description="Resolved entity code for the major or specialization")
    entity_name: str = Field(description="Resolved entity name for the major or specialization")
    entity_type: MajorInterestEntityType = Field(description="Resolved entity type")
    signal_type: MajorInterestSignalType = Field(description="Semantic signal extracted from the latest turn")
    polarity: MajorInterestPolarity = Field(description="Semantic direction of the signal")
    preference_order: int | None = Field(default=None, description="Preference rank such as nguyen vong 1/2/3 when provided")
    confidence: float = Field(default=0.0, description="LLM confidence for this semantic signal")
    evidence: str | None = Field(default=None, description="Short quote or evidence snippet from the user message")
    turn_index: int | None = Field(default=None, description="Absolute user turn index when the event was created")
    source_text: str | None = Field(default=None, description="Original user message that produced this event")
    related_entity_code: str | None = Field(default=None, description="Peer entity code for compare/replace signals")
    related_entity_name: str | None = Field(default=None, description="Peer entity name for compare/replace signals")
    notes: str | None = Field(default=None, description="Debug notes or fallback reason for this event")


class MajorInterestScore(BaseModel):
    entity_code: str = Field(description="Resolved entity code for the major or specialization")
    entity_name: str = Field(description="Resolved entity name for the major or specialization")
    entity_type: MajorInterestEntityType = Field(description="Resolved entity type")
    interest_score: int = Field(default=0, description="Aggregated interest score in the range 0..100")
    status: str = Field(default="weak_signal", description="Interest status bucket derived from the score")
    rank: int = Field(default=0, description="Rank in the sorted interest table")
    event_count: int = Field(default=0, description="How many events contributed to this entity")
    last_turn_index: int | None = Field(default=None, description="Most recent turn index mentioning this entity")
    in_interested_majors: bool = Field(default=False, description="Whether the entity is currently promoted")
    reason_summary: str | None = Field(default=None, description="Short debug explanation for the current ranking")


def combine_major_views(
    potential_majors: list[dict[str, str]] | None,
    interested_majors: list[dict[str, str]] | None,
) -> list[dict[str, str]]:
    combined: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for source in (interested_majors or [], potential_majors or []):
        if not isinstance(source, dict):
            items = source if isinstance(source, list) else []
        else:
            items = [source]

        for item in items:
            if not isinstance(item, dict):
                continue
            for entity_code, entity_name in item.items():
                key = (str(entity_code).strip(), str(entity_name).strip())
                if key in seen:
                    continue
                seen.add(key)
                combined.append({entity_code: entity_name})

    return combined


class UserState(BaseModel):
    role: str | None = Field(default=None, description="User's role")
    topic: list[str] = Field(default_factory=list)
    major: list[dict[str, str]] = Field(
        default_factory=list, description="Specific majors/fields mentioned, format: [{code: name}]"
    )
    potential_majors: list[dict[str, str]] = Field(
        default_factory=list,
        description="Majors or specializations the user has mentioned or shown interest in during the conversation",
    )
    interested_majors: list[dict[str, str]] = Field(
        default_factory=list,
        description="Ranked majors or specializations that have crossed the registration-interest threshold",
    )
    major_interest_events: list[MajorInterestEvent] = Field(
        default_factory=list, description="Semantic major-interest events extracted from the conversation"
    )
    major_interest_scores: list[MajorInterestScore] = Field(
        default_factory=list, description="Current major-interest ranking derived from the event history"
    )
    is_spam: bool | None = Field(default=False, description="Whether the input is considered spam")
    is_asking_agent_identity: bool | None = Field(
        default=None, description="Whether the user is asking about the agent's identity, role, or capabilities"
    )
    personalization: list[str] = Field(
        default_factory=list, description="User's preferences and desired careers for personalization"
    )
    is_requested_advise_major: bool | None = Field(
        default=None, description="Whether the user has requested advice on specific major"
    )
    is_requested_advisor: bool | None = Field(
        default=None, description="Whether the user has requested to meet an advisor"
    )
    is_confirm_information_to_fill_form: bool | None = Field(
        default=None, description="Whether the user has confirmed information to fill form"
    )
    is_requested_submit_application_now: bool = Field(
        default=False, description="Whether the current user message asks to submit the admission application now"
    )
    is_asking_admission_criteria: bool = Field(
        default=False, description="Whether the current user message asks about admission criteria or requirements"
    )
    is_asking_comparison: bool = Field(
        default=False, description="Whether the current user message compares GDU with another option"
    )
    is_asking_roadmap: bool = Field(
        default=False, description="Whether the current user message asks about curriculum roadmap or learning path"
    )
    is_asking_application_process: bool = Field(
        default=False, description="Whether the current user message asks about application or enrollment steps"
    )
    is_asking_deadline: bool = Field(
        default=False, description="Whether the current user message asks about deadlines or timelines"
    )
    confirmed_select_major: dict[str, str] | None = Field(
        default=None, description="User's confirmed major for form filling"
    )
    selected_school_option: int | None = Field(
        default=None, description="User's selected school option from a list (1-5)"
    )

    # Personal Information
    student_profile: PersonalInfo | None = Field(
        default_factory=PersonalInfo, description="User's personal information"
    )

    @model_validator(mode="before")
    @classmethod
    def _sync_major_alias_before_validation(cls, data):
        if not isinstance(data, dict):
            return data

        major = data.get("major")
        potential_majors = data.get("potential_majors")
        interested_majors = data.get("interested_majors")

        if isinstance(major, list) and not isinstance(potential_majors, list):
            data["potential_majors"] = list(major)
        elif isinstance(potential_majors, list) and not isinstance(major, list):
            data["major"] = combine_major_views(potential_majors, interested_majors if isinstance(interested_majors, list) else [])
        else:
            data.setdefault("major", [])
            data.setdefault("potential_majors", [])

        data.setdefault("interested_majors", [])
        data.setdefault("major_interest_events", [])
        data.setdefault("major_interest_scores", [])
        return data

    @model_validator(mode="after")
    def _sync_major_alias_after_validation(self):
        if self.major and not self.potential_majors and not self.interested_majors:
            self.potential_majors = list(self.major)
        self.major = combine_major_views(self.potential_majors, self.interested_majors)
        return self
