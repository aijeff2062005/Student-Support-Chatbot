from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

SEGMENT_KEYWORD_FIELD_NAMES = (
    "motivation_keywords",
    "tuition_sensitive_keywords",
    "core_values_keywords",
    "studying_goals_keywords",
    "environment_keywords",
    "interaction_frequency_keywords",
    "preferred_channel_keywords",
    "financial_behavior_keywords",
    "churn_risk_signals_keywords",
)

_SCHEMA_ARTIFACT_KEYWORDS = {
    field_name
    for field_name in SEGMENT_KEYWORD_FIELD_NAMES
} | {
    field_name.replace("_", " ")
    for field_name in SEGMENT_KEYWORD_FIELD_NAMES
}

KeywordValue = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]


def _keyword_field(description: str, example: list[str]) -> Any:
    return Field(
        default_factory=list,
        description=description,
        json_schema_extra={
            "example": example,
            "maxItems": 3,
        },
    )


def _keyword_match_key(value: str) -> str:
    return " ".join(value.lower().strip().split())


def _is_schema_artifact_keyword(value: str) -> bool:
    return _keyword_match_key(value) in _SCHEMA_ARTIFACT_KEYWORDS


class SegmentAgentOutput(BaseModel):
    """Structured keyword extraction output for customer segmentation."""

    model_config = ConfigDict(extra="ignore")

    reasoning_explain: str = Field(
        default="",
        description=(
            "Giải thích ngắn vì sao tin nhắn hiện tại có hoặc không có tín hiệu segmentation. "
            "Không dùng field này làm keyword hoặc gửi sang segment API."
        ),
        max_length=500,
    )
    original_message: str = Field(
        default="",
        description=(
            "Tin nhắn người dùng hiện tại được dùng để trích xuất keyword. "
            "Lấy từ original_message của message_rewrite_agent khi có, không dùng lịch sử."
        ),
        max_length=50000,
    )
    motivation_keywords: list[KeywordValue] = _keyword_field(
        "Động lực chính thúc đẩy người dùng học hoặc chọn trường/ngành. Tối đa 3 cụm ngắn, rõ nghĩa.",
        ["thu nhập ổn định", "dễ xin việc"],
    )
    tuition_sensitive_keywords: list[KeywordValue] = _keyword_field(
        "Tín hiệu nhạy cảm với học phí, chi phí, khả năng chi trả. Chỉ giữ cụm ngắn thực sự thể hiện mối quan tâm tài chính.",
        ["học phí cao quá", "chi phí vừa phải"],
    )
    core_values_keywords: list[KeywordValue] = _keyword_field(
        "Giá trị cốt lõi người dùng ưu tiên khi chọn trường/ngành, ví dụ uy tín, thực hành, linh hoạt.",
        ["uy tín", "thực hành nhiều"],
    )
    studying_goals_keywords: list[KeywordValue] = _keyword_field(
        "Mục tiêu học tập hoặc đầu ra mong muốn như ra trường dễ xin việc, lấy bằng nhanh, chuyển ngành.",
        ["dễ xin việc", "ra trường sớm"],
    )
    environment_keywords: list[KeywordValue] = _keyword_field(
        "Ưu tiên về môi trường học như gần nhà, năng động, có ký túc xá, lớp nhỏ.",
        ["gần nhà", "môi trường năng động"],
    )
    interaction_frequency_keywords: list[KeywordValue] = _keyword_field(
        "Tín hiệu về tần suất tương tác mong muốn như cần tư vấn thường xuyên, cập nhật liên tục.",
        ["tư vấn thường xuyên", "cập nhật liên tục"],
    )
    preferred_channel_keywords: list[KeywordValue] = _keyword_field(
        "Kênh liên lạc người dùng muốn dùng như zalo, facebook, goi dien, email.",
        ["zalo", "gọi điện"],
    )
    financial_behavior_keywords: list[KeywordValue] = _keyword_field(
        "Hành vi tài chính như trả góp, săn học bổng, cân nhắc chi phí dài hạn.",
        ["trả góp", "săn học bổng"],
    )
    churn_risk_signals_keywords: list[KeywordValue] = _keyword_field(
        "Dấu hiệu do dự, trì hoãn, so sánh nhiều hoặc rủi ro rời bỏ như còn phân vân, sợ không theo kịp.",
        ["còn phân vân", "sợ không theo kịp"],
    )

    @field_validator(
        "motivation_keywords",
        "tuition_sensitive_keywords",
        "core_values_keywords",
        "studying_goals_keywords",
        "environment_keywords",
        "interaction_frequency_keywords",
        "preferred_channel_keywords",
        "financial_behavior_keywords",
        "churn_risk_signals_keywords",
        mode="before",
    )
    @classmethod
    def _coerce_keyword_list(cls, value: Any) -> list[str]:
        if value is None:
            return []

        if isinstance(value, str):
            raw_values = [value]
        elif isinstance(value, (list, tuple, set)):
            raw_values = list(value)
        else:
            return []

        normalized: list[str] = []
        for item in raw_values:
            if item is None:
                continue
            text = str(item).strip()
            if not text:
                continue
            if _is_schema_artifact_keyword(text):
                continue
            normalized.append(text[:80])

        return normalized
