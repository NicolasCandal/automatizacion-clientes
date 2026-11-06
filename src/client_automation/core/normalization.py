"""Name normalization and holder extraction using the existing domain rules."""

from __future__ import annotations

import re
import unicodedata
from typing import Final

from .input_formats import RELATIONSHIP_PREFIXES
from .models import NameKeys

_PUNCTUATION_PATTERN: Final[re.Pattern[str]] = re.compile(r"[^\w\s]", re.UNICODE)
_PATTERN_SPACES: Final[re.Pattern[str]] = re.compile("\\s+")
_AND_OR_PATTERN: Final[re.Pattern[str]] = re.compile("\\s+(?:and/or|y/o)\\s+", re.IGNORECASE)
_AND_SEPARATOR_PATTERN: Final[re.Pattern[str]] = re.compile("\\s+(?:and|y)\\s+", re.IGNORECASE)
_PATTERN_PARENTHESES: Final[re.Pattern[str]] = re.compile("\\(([^)]*)\\)")
_RELATIONSHIP_PREFIX_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(?:" + "|".join(re.escape(prefix) for prefix in RELATIONSHIP_PREFIXES) + r")\s+",
    re.IGNORECASE,
)


def strip_accents(text: str) -> str:
    """Apply NFKD decomposition and remove combining marks."""
    text_decomposed = unicodedata.normalize("NFKD", text)
    return "".join(
        character for character in text_decomposed if not unicodedata.combining(character)
    )


def normalize(text: str) -> str:
    """Lowercase, decompose Unicode, replace punctuation, and collapse whitespace."""
    text_without_accents = strip_accents(text.strip().lower())
    text_without_punctuation = _PUNCTUATION_PATTERN.sub(" ", text_without_accents)
    return _PATTERN_SPACES.sub(" ", text_without_punctuation).strip()


def sorted_name_key(text: str) -> str:
    """Build a key independent of the ordering of name and surname tokens."""
    return " ".join(sorted(normalize(text).split()))


def generate_name_keys(raw_name: str) -> NameKeys:
    """Generate comparison keys while preserving the exact original text."""
    normalized_name = normalize(raw_name)
    return NameKeys(
        raw=raw_name,
        normalized=normalized_name,
        sorted_name_key=sorted_name_key(normalized_name),
    )


def extract_holders(raw_name: str) -> tuple[str, ...]:
    """Split cell holders using the documented separator and relationship rules."""
    text = raw_name.strip()
    holders_parentheses: list[str] = []
    matching = _PATTERN_PARENTHESES.search(text)
    if matching is not None:
        content = matching.group(1).strip()
        text = (text[: matching.start()] + text[matching.end() :]).strip()
        content_without_relationship = _RELATIONSHIP_PREFIX_PATTERN.sub("", content).strip()
        if content_without_relationship and content_without_relationship != content:
            holders_parentheses.append(content_without_relationship)

    if _AND_OR_PATTERN.search(text):
        parts = _AND_OR_PATTERN.split(text)
    elif "/" in text:
        parts = text.split("/")
    else:
        parts_with_and = _AND_SEPARATOR_PATTERN.split(text)
        parts = parts_with_and if len(parts_with_and) == 2 else [text]

    holders = [part.strip() for part in parts if part.strip()]
    holders.extend(holders_parentheses)
    return tuple(holders)
