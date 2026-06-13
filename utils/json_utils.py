"""JSON utility helpers shared across callbacks and agents."""

import json
import re
from typing import Any


def _strip_markdown_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _json_candidates(text: str) -> list[str]:
    """Return balanced JSON object/array substrings found inside text."""
    candidates: list[str] = []
    start_chars = {"{": "}", "[": "]"}

    for start_index, char in enumerate(text):
        if char not in start_chars:
            continue

        stack = [start_chars[char]]  
        in_string = False
        escaped = False

        for index in range(start_index + 1, len(text)):
            current = text[index]

            if in_string:
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == '"':
                    in_string = False
                continue

            if current == '"':
                in_string = True
            elif current in start_chars:
                stack.append(start_chars[current])
            elif stack and current == stack[-1]:
                stack.pop()
                if not stack:
                    candidates.append(text[start_index : index + 1])
                    break

    return candidates


def extract_json(text: str) -> Any:
    """Extract JSON from LLM response text, handling markdown code fences and think blocks.

    Handles:
        - Plain JSON: {"key": "value"}
        - Fenced JSON: ```json\\n{"key": "value"}\\n```
        - Fenced no lang: ```\\n{"key": "value"}\\n```
        - Think blocks: <think>...</think>\\n{"key": "value"}
        - Extra text before/after JSON: H{"key": "value"}
        - JSON double-encoded as a string: "{\"key\": \"value\"}"

    Args:
        text: Raw LLM output string.

    Returns:
        Parsed JSON value.

    Raises:
        json.JSONDecodeError: If text cannot be parsed as JSON after stripping.
    """
    text = _strip_markdown_fence(str(text))

    last_error: json.JSONDecodeError | None = None

    for _ in range(3):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as error:
            last_error = error
            break

        if isinstance(parsed, str):
            nested = _strip_markdown_fence(parsed)
            if nested == text:
                return parsed
            text = nested
            continue

        return parsed

    for candidate in _json_candidates(text):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as error:
            last_error = error
            continue

        if isinstance(parsed, str):
            return extract_json(parsed)
        return parsed

    if last_error is not None:
        raise last_error
    return json.loads(text)
