"""Pure recognition of brokers in parenthesized contact-name text."""

from __future__ import annotations

import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from .models import BrokerName
from .normalization import normalize

DEFAULT_BROKER_TABLE: Final[Mapping[str, str]] = MappingProxyType(
    {
        "iol": "IOL",
        "invertironline": "IOL",
        "bull": "Bull",
        "bm": "Bull",
        "bull market": "Bull",
        "balanz": "Balanz",
        "ppi": "PPI",
        "portfolio personal": "PPI",
        "inviu": "Inviu",
    }
)

_PATTERN_PARENTHESES: Final[re.Pattern[str]] = re.compile("\\(([^)]*)\\)")
_PATTERN_SEPARATOR_BROKERS: Final[re.Pattern[str]] = re.compile(
    "\\s*/\\s*|\\s+(?:and|y)\\s+|\\s*-\\s*|\\s*,\\s*", re.IGNORECASE
)
_PATTERN_SPACES: Final[re.Pattern[str]] = re.compile("\\s+")


def extract_brokers_and_name(
    raw_name: str,
    table_brokers: Mapping[str, str] | None = None,
) -> BrokerName:
    """Separate the name, recognized sheets, and unrelated parenthesized text."""
    table = DEFAULT_BROKER_TABLE if table_brokers is None else table_brokers
    contents_parentheses = _PATTERN_PARENTHESES.findall(raw_name)
    name_without_parentheses = _PATTERN_PARENTHESES.sub("", raw_name).strip()
    name_without_parentheses = _PATTERN_SPACES.sub(" ", name_without_parentheses)

    sheets: list[str] = []
    sheets_seen: set[str] = set()
    noise: list[str] = []
    for content in contents_parentheses:
        for token in _PATTERN_SEPARATOR_BROKERS.split(content):
            token_clean = token.strip()
            if not token_clean:
                continue
            sheet = table.get(normalize(token_clean))
            if sheet is None:
                noise.append(token_clean)
            elif sheet not in sheets_seen:
                sheets_seen.add(sheet)
                sheets.append(sheet)

    return BrokerName(
        name_without_parentheses=name_without_parentheses,
        sheets=tuple(sheets),
        noise=tuple(noise),
    )
