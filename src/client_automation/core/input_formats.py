"""Locale-specific input labels accepted by the spreadsheet and name parsers."""

from typing import Final

CLIENT_COLUMN_HEADERS: Final[frozenset[str]] = frozenset(
    {"client", "clients", "name", "cliente", "clientes", "nombre"}
)

RELATIONSHIP_PREFIXES: Final[tuple[str, ...]] = (
    "wife of",
    "husband of",
    "son of",
    "daughter of",
    "wife",
    "husband",
    "son",
    "daughter",
    "esposa de",
    "esposo de",
    "hijo de",
    "hija de",
    "esposa",
    "esposo",
    "hijo",
    "hija",
)
