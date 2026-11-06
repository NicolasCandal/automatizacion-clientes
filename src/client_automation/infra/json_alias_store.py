"""Typed aliases persisted in the documented JSON representation."""

import logging
from pathlib import Path

from client_automation.core.aliases import reconcile_alias
from client_automation.core.errors import (
    ConcurrencyError,
    PersistenceError,
    StructureError,
)
from client_automation.core.models import AliasDestination, ContactAlias
from client_automation.infra.json_storage import JsonValue, read_json, save_json

LOGGER = logging.getLogger(__name__)


class JsonAliasStore:
    """Keep an alias session in memory and persist only when save is called."""

    def __init__(self, path: Path) -> None:
        """Load and validate existing aliases without modifying their file."""
        self._path = path
        self._persisted = _load_aliases(path)
        self._aliases: dict[str, ContactAlias] = (
            {} if self._persisted is None else self._persisted.copy()
        )

    def find_by_name(self, raw_name: str) -> ContactAlias | None:
        """Find an exact original name without changing the session or disk."""
        return self._aliases.get(raw_name)

    def __len__(self) -> int:
        """Return the session association count without accessing files."""
        return len(self._aliases)

    def find_by_phone(self, phone: str) -> tuple[ContactAlias, ...]:
        """Return all exact-phone associations in stable order."""
        return tuple(
            self._aliases[name]
            for name in sorted(self._aliases)
            if self._aliases[name].phone == phone
        )

    def register(self, alias: ContactAlias) -> str | None:
        """Accept an alias or return a conflict warning without personal information."""
        preserved, warning = reconcile_alias(self._aliases.get(alias.raw_name), alias)
        self._aliases[alias.raw_name] = preserved
        if warning is not None:
            LOGGER.warning(warning)
        return warning

    def save(self) -> None:
        """Save atomically after checking that the file has not changed externally."""
        if _load_aliases(self._path) != self._persisted:
            raise ConcurrencyError(
                "The alias file changed. Open a new session and review pending changes."
            )

        document: dict[str, JsonValue] = {}
        for name, alias in self._aliases.items():
            people: list[JsonValue] = []
            for destination in alias.people:
                sheets: list[JsonValue] = list(sorted(destination.sheets))
                people.append({"person_id": destination.person_id, "sheets": sheets})
            document[name] = {"people": people, "phone": alias.phone}

        save_json(self._path, document)
        self._persisted = self._aliases.copy()


def _load_aliases(path: Path) -> dict[str, ContactAlias] | None:
    try:
        document = read_json(path)
    except PersistenceError as cause:
        if isinstance(cause.__cause__, FileNotFoundError):
            return None
        raise

    if not isinstance(document, dict):
        raise StructureError("The alias document must be a JSON object.")
    return {name: _decode_alias(name, input) for name, input in document.items()}


def _decode_alias(name: str, input: JsonValue) -> ContactAlias:
    if not isinstance(input, dict) or not {"people"} <= input.keys() <= {
        "people",
        "phone",
    }:
        raise StructureError("The alias entry has missing or unknown fields.")
    people = input["people"]
    phone = input.get("phone")
    if not isinstance(people, list) or not (phone is None or isinstance(phone, str)):
        raise StructureError("The alias entry has invalid field types.")
    return ContactAlias(
        raw_name=name,
        phone=phone,
        people=tuple(_decode_destination(person) for person in people),
    )


def _decode_destination(input: JsonValue) -> AliasDestination:
    if not isinstance(input, dict) or input.keys() != {"person_id", "sheets"}:
        raise StructureError("The alias destination has missing or unknown fields.")
    person_id = input["person_id"]
    sheets = input["sheets"]
    if not isinstance(person_id, str) or not isinstance(sheets, list):
        raise StructureError("The alias destination has invalid field types.")
    sheets_validated: set[str] = set()
    for sheet in sheets:
        if not isinstance(sheet, str) or sheet in sheets_validated:
            raise StructureError("Alias sheets must be strings without duplicates.")
        sheets_validated.add(sheet)
    return AliasDestination(person_id=person_id, sheets=frozenset(sheets_validated))
