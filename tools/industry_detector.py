"""LLM-based industry detector from user state."""

import logging

from pydantic import BaseModel, Field

from configs.config_service import get_settings
from utils.reranker_helper import rerank_industry

logger = logging.getLogger(__name__)

settings = get_settings()

# Valid industries from OfferingRequest schema
VALID_INDUSTRIES = [
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


class IndustryDetectionResult(BaseModel):
    """Result from industry detection."""

    industries: list[str] = Field(description="List of detected industries from VALID_INDUSTRIES")
    confidence: float = Field(default=0.8, ge=0.0, le=1.0, description="Confidence score (0-1)")


def detect_industry_from_user_state(major: list[str]) -> list[str] | None:

    majors_to_process = []
    if isinstance(major, str):
        majors_to_process.append(major)
    elif isinstance(major, list):
        majors_to_process.extend(major)

    all_industries = []

    for m in majors_to_process:
        if not m:
            continue
        res = rerank_industry(query=m, candidate_industries=VALID_INDUSTRIES)

        if res and isinstance(res, dict):
            all_industries.append(res["industry"])
        elif res and isinstance(res, list):
            all_industries.extend(
                [r["industry"] for r in res]
            )  # Take all or top 1? Original took top 1 logic implicitly via extraction

    # Deduplicate
    unique_industries = list(set(all_industries))

    return unique_industries if unique_industries else None
