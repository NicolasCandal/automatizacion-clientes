"""Versioned catalog persistence without accessing or changing its source."""

import logging
from datetime import datetime
from pathlib import Path
from typing import Final

from client_automation.core.errors import DomainValidationError, PersistenceError, StructureError
from client_automation.core.models import (
    ClientCatalog,
    ClientLocation,
    ClientOccurrence,
    ClientPerson,
    SheetInfo,
)
from client_automation.infra.json_storage import JsonValue, read_json, save_json

LOGGER = logging.getLogger(__name__)
CATALOG_SCHEMA_VERSION: Final[int] = 1


class JsonCatalogStore:
    """Implement CatalogStore with a versioned JSON document."""

    def __init__(self, path: Path) -> None:
        """Accept the configured path without disk access or directory creation."""
        self._path = path

    def read(self) -> ClientCatalog | None:
        """Load a valid catalog; return None only when the file is absent."""
        try:
            document = read_json(self._path)
        except PersistenceError as cause:
            if isinstance(cause.__cause__, FileNotFoundError):
                return None
            raise
        catalog = _decode_catalog(document)
        LOGGER.info(
            "Catalog validated: %s people and %s sheets.",
            len(catalog.people),
            len(catalog.sheets),
        )
        return catalog

    def save(self, catalog: ClientCatalog) -> None:
        """Save the complete catalog through atomic replacement."""
        sheets: list[JsonValue] = [
            {
                "name": sheet.name,
                "sheet_id": sheet.sheet_id,
                "name_column": sheet.name_column,
                "first_week_column": sheet.first_week_column,
                "last_week_column": sheet.last_week_column,
                "last_client_row": sheet.last_client_row,
            }
            for sheet in catalog.sheets
        ]
        people: list[JsonValue] = [_encode_person(person) for person in catalog.people]
        save_json(
            self._path,
            {
                "schema_version": CATALOG_SCHEMA_VERSION,
                "source_id": catalog.source_id,
                "revision": catalog.revision,
                "generated_at": catalog.generated_at.isoformat(),
                "sheets": sheets,
                "people": people,
            },
        )
        LOGGER.info(
            "Catalog saved: %s people and %s sheets.",
            len(catalog.people),
            len(catalog.sheets),
        )


def _decode_catalog(document: JsonValue) -> ClientCatalog:
    if not isinstance(document, dict) or "schema_version" not in document:
        raise StructureError("The catalog has no schema version. Rebuild it from the source.")
    version = document["schema_version"]
    if type(version) is not int or version != CATALOG_SCHEMA_VERSION:
        raise StructureError("The catalog schema version is unsupported.")
    input = _read_object(
        document,
        {"schema_version", "source_id", "revision", "generated_at", "sheets", "people"},
    )
    try:
        generated_at = datetime.fromisoformat(_read_text(input["generated_at"]))
    except ValueError as cause:
        raise DomainValidationError(
            "The catalog generation timestamp is not a valid ISO instant."
        ) from cause
    return ClientCatalog(
        source_id=_read_text(input["source_id"]),
        revision=_read_text(input["revision"]),
        generated_at=generated_at,
        sheets=tuple(_decode_sheet(sheet) for sheet in _read_list(input["sheets"])),
        people=tuple(_decode_person(person) for person in _read_list(input["people"])),
    )


def _decode_sheet(document: JsonValue) -> SheetInfo:
    input = _read_object(
        document,
        {
            "name",
            "sheet_id",
            "name_column",
            "first_week_column",
            "last_week_column",
            "last_client_row",
        },
    )
    return SheetInfo(
        name=_read_text(input["name"]),
        sheet_id=_read_integer(input["sheet_id"]),
        name_column=_read_integer(input["name_column"]),
        first_week_column=_read_column_optional(input["first_week_column"]),
        last_week_column=_read_column_optional(input["last_week_column"]),
        last_client_row=_read_integer(input["last_client_row"]),
    )


def _decode_person(document: JsonValue) -> ClientPerson:
    input = _read_object(
        document, {"person_id", "sorted_name_key", "canonical_name", "holders", "occurrences"}
    )
    return ClientPerson(
        person_id=_read_text(input["person_id"]),
        sorted_name_key=_read_text(input["sorted_name_key"]),
        canonical_name=_read_text(input["canonical_name"]),
        holders=tuple(_read_text(holder) for holder in _read_list(input["holders"])),
        occurrences=tuple(
            _decode_occurrence(occurrence) for occurrence in _read_list(input["occurrences"])
        ),
    )


def _decode_occurrence(document: JsonValue) -> ClientOccurrence:
    input = _read_object(document, {"sheet", "row", "raw_name"})
    return ClientOccurrence(
        location=ClientLocation(sheet=_read_text(input["sheet"]), row=_read_integer(input["row"])),
        raw_name=_read_text(input["raw_name"]),
    )


def _encode_person(person: ClientPerson) -> dict[str, JsonValue]:
    holders: list[JsonValue] = list(person.holders)
    occurrences: list[JsonValue] = [
        {
            "sheet": occurrence.location.sheet,
            "row": occurrence.location.row,
            "raw_name": occurrence.raw_name,
        }
        for occurrence in person.occurrences
    ]
    return {
        "person_id": person.person_id,
        "sorted_name_key": person.sorted_name_key,
        "canonical_name": person.canonical_name,
        "holders": holders,
        "occurrences": occurrences,
    }


def _read_object(value: JsonValue, fields: set[str]) -> dict[str, JsonValue]:
    if not isinstance(value, dict) or value.keys() != fields:
        raise StructureError("Catalog objects have missing or unknown fields.")
    return value


def _read_list(value: JsonValue) -> list[JsonValue]:
    if not isinstance(value, list):
        raise StructureError("A catalog list field has an incorrect type.")
    return value


def _read_text(value: JsonValue) -> str:
    if not isinstance(value, str):
        raise StructureError("A catalog text field has an incorrect type.")
    return value


def _read_integer(value: JsonValue) -> int:
    if type(value) is not int:
        raise StructureError("A catalog index must be an integer without type coercion.")
    return value


def _read_column_optional(value: JsonValue) -> int | None:
    return None if value is None else _read_integer(value)
