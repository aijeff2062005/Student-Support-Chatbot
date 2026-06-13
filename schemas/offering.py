from typing import Literal

from pydantic import BaseModel, Field


class ScoreRecord(BaseModel):
    """Score information for major recommendation."""

    academic_transcript: float | None = Field(default=None, description="Academic transcript score")
    aptitude_test: float | None = Field(default=None, description="Aptitude test score")
    high_school_graduation_exam: float | None = Field(default=None, description="High school graduation exam score")


class OfferingRequest(BaseModel):
    """Request schema for top-majors API."""

    major_codes: list[str] | None = Field(default=None, description="List of selected/mentioned major codes")
    expected_industries: list[
        Literal[
            "Công Nghệ Thông Tin",
            "Truyền Thông - Marketing",
            "Kinh Doanh - Bán Hàng",
            "Du Lịch - Nhà Hàng - Khách Sạn",
            "Logistics & Chuỗi Cung Ứng",
            "Tài Chính - Ngân Hàng",
            "Luật",
            "Ngôn Ngữ Học",
            "Khoa Học Xã hội & Nhân Văn",
            "Y Học",
        ]
    ] = Field(description="List of industries the user is interested in")
    score: ScoreRecord | None = Field(
        default=None, description="Score information (graduation exam, transcript, aptitude test)"
    )
    top_k: int = Field(default=3, ge=1, le=25, description="Number of top majors to return")


class TrendInfo(BaseModel):
    """Trend information for a major."""

    salary_range: list[int] = Field(default_factory=list, description="Salary range [min, max]")
    market_demand: str = Field(default="", description="Market demand description")
    international_opportunity: str = Field(default="", description="International opportunity description")
    reference: list[str] = Field(default_factory=list, description="Reference links")


class OfferingItem(BaseModel):
    """Single major/offering item in response."""

    major_id: str = Field(description="Major ID code")
    major_name: str = Field(description="Major name")
    successful_admission_rates: float = Field(default=0.0, description="Successful admission rate")
    trend_info: TrendInfo = Field(description="Trend and career information")


class OfferingResponse(BaseModel):
    """Response schema from top-majors API."""

    clubs: list[str] = Field(default_factory=list, description="List of recommended student clubs")
    majors: list[OfferingItem] = Field(default_factory=list, description="List of recommended majors")
