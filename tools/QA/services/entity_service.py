"""Entity Extraction Service.

Provides centralized entity extraction for questions.
All entity extraction must go through this service.
"""

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


class EntityService:
    """Service for extracting entities from questions."""

    _instance: "EntityService | None" = None

    def __new__(cls):
        """Singleton pattern."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        """Initialize entity service."""
        if self._initialized:
            return

        # Define entity patterns for university admission domain
        self.patterns = {
            "major": [
                r"(công nghệ thông tin|cntt|kế toán|kinh tế|y khoa|điều dưỡng|xây dựng|điện|cơ|toán|vật lý|hóa|sinh)",
                r"ngành\s+([^,.\?]{5,30})",
            ],
            "year": [r"năm\s+(\d{4})", r"(\d{4})"],
            "topic": [
                r"(học phí|điều kiện|hồ sơ|tuyển sinh|xét tuyển|học bạ|thi thpt|thẳng|xét học bạ|xét tuyển thẳng)",
            ],
            "location": [
                r"(hà nội|tp\.? ?hcm|tp\.? ?hồ chí minh|đà nẵng|hải phòng|cần thơ|quảng ninh|bắc giang|bắc ninh)",
            ],
            "method": [
                r"(học bạ|thi thpt|xét tuyển thẳng|ưu tiên|đối tượng)",
            ],
            "priority_group": [
                r"(khu vực \d+|đối tượng ưu tiên|ưu tiên tuyển)",
            ],
        }

        self._initialized = True
        logger.info(" EntityService initialized")

    def extract_entities(self, question: str) -> dict[str, Any]:
        """Extract entities from question.

        This is the ONLY way to extract entities in the system.
        All entity extraction must go through this method.

        Args:
            question: Question text

        Returns:
            Dict with extracted entities
        """
        if not question:
            return {}

        entities = {}
        question_lower = question.lower()

        # Extract each entity type
        for entity_type, patterns in self.patterns.items():
            for pattern in patterns:
                matches = re.findall(pattern, question_lower)
                if matches:
                    # Get first match (could be list or string)
                    match = matches[0] if isinstance(matches[0], str) else matches[0][0]
                    entities[entity_type] = match
                    logger.debug(f"Extracted {entity_type}: {match}")
                    break  # Use first pattern that matches

        # Extract keywords (4+ character words, excluding stopwords)
        stopwords = {
            "là",
            "và",
            "hay",
            "với",
            "từ",
            "đến",
            "qua",
            "được",
            "có",
            "này",
            "việc",
            "cái",
            "gì",
            "nào",
            "không",
            "đã",
            "sẽ",
            "đang",
            "về",
        }
        words = re.findall(r"\b\w{4,}\b", question_lower)
        keywords = [w for w in words if w not in stopwords]
        if keywords:
            entities["keywords"] = keywords[:5]  # Top 5 keywords

        logger.info(f"Extracted entities: {entities}")
        return entities

    def get_entity_patterns(self) -> dict[str, list[str]]:
        """Get available entity patterns (for debugging/documentation)."""
        return {k: [p[:30] + "..." if len(p) > 30 else p for p in v] for k, v in self.patterns.items()}

    def format_entities_display(self, entities: dict[str, Any]) -> str:
        """Format entities for user-friendly display.

        Args:
            entities: Extracted entities dict

        Returns:
            Formatted string for display
        """
        if not entities:
            return ""

        parts = []
        if "major" in entities:
            parts.append(f"Ngành: {entities['major']}")
        if "year" in entities:
            parts.append(f"Năm: {entities['year']}")
        if "topic" in entities:
            parts.append(f"Chủ đề: {entities['topic']}")
        if "location" in entities:
            parts.append(f"Địa điểm: {entities['location']}")
        if "method" in entities:
            parts.append(f"Phương thức: {entities['method']}")

        return " | ".join(parts) if parts else ""


def get_entity_service() -> EntityService:
    """Get singleton EntityService instance."""
    return EntityService()
