"""Validated conversion of legacy dictionaries to current domain models."""

from datetime import UTC, datetime
from typing import NotRequired, Required, TypedDict, cast

from client_automation.core.errors import StructureError
from client_automation.core.models import (
    AliasDestination,
    ClientCatalog,
    ClientLocation,
    ClientOccurrence,
    ClientPerson,
    ContactAlias,
    ContactResult,
    IgnoredContact,
    PendingContact,
    ResolvedContact,
    SheetInfo,
    WeeklyContact,
)


class LegacyOccurrence(TypedDict):
    """Location and original name in the legacy representation."""

    sheet: str
    row: int
    raw_name: str


class LegacyPerson(TypedDict):
    """Legacy person whose identity is preserved during conversion."""

    id: str
    sorted_name_key: str
    canonical_name: str
    holders: list[str]
    occurrences: list[LegacyOccurrence]


class LegacySheet(TypedDict):
    """Legacy sheet structure including the last client row for gap detection."""

    sheet_id: int
    name_column: int
    first_week_column: int | None
    last_week_column: int | None
    last_header_week: str | None
    client_rows: int
    last_client_row: int


class LegacyCatalog(TypedDict, total=False):
    """Legacy catalog view that is never persisted as a current catalog."""

    people: Required[list[LegacyPerson]]
    spreadsheet_id: str
    revision: str
    generated_at: str
    sheets: dict[str, LegacySheet]


class LegacyDestination(TypedDict):
    """Person and selected sheets from a legacy alias."""

    person_id: str
    sheets: list[str]


class LegacyAlias(TypedDict):
    """Legacy view of a typed learned association."""

    people: list[LegacyDestination]
    phone: str | None


class LegacyContact(TypedDict):
    """Input metadata without message bodies."""

    raw_name: str
    last_activity_date: str
    phone: NotRequired[str | None]
    is_group: NotRequired[bool]


class LegacyCandidate(TypedDict):
    """Summarized matching candidate for the legacy interface."""

    person_id: str
    name: str
    score: float


class LegacyResult(TypedDict, total=False):
    """Legacy result view whose locations preserve every row in a sheet."""

    kind: str
    raw_name: str
    person_id: str
    sheets: list[str]
    row_by_sheet: dict[str, int]
    locations: list[dict[str, str | int]]
    method: str
    score: float
    reason: str
    candidates: list[LegacyCandidate]
    warnings: list[str]


class LegacyResolution(TypedDict):
    """Grouped legacy result collections."""

    resolved: list[LegacyResult]
    pending: list[LegacyResult]
    ignored: list[LegacyResult]
    warnings: list[str]


def require_object(value: object) -> dict[str, object]:
    """Require an object with string keys without hiding invalid input."""
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise StructureError("The legacy representation requires an object with string keys.")
    return cast(dict[str, object], value)


def require_list(value: object) -> list[object]:
    """Require a JSON list before conversion."""
    if not isinstance(value, list):
        raise StructureError("The legacy representation requires a list.")
    return cast(list[object], value)


def text(value: object) -> str:
    """Require text without inferring identities."""
    if not isinstance(value, str):
        raise StructureError("The legacy representation requires text.")
    return value


def integer(value: object) -> int:
    """Require integer indices and reject booleans."""
    if type(value) is not int:
        raise StructureError("The legacy representation requires an integer index.")
    return value


def catalog_for_matching(input: object) -> ClientCatalog:
    """Preserve identities and occurrences in a memory-only matching index."""
    document = require_object(input)
    people: list[ClientPerson] = []
    last: dict[str, int] = {}
    for value in require_list(document.get("people")):
        person = require_object(value)
        occurrences: list[ClientOccurrence] = []
        for value_occurrence in require_list(person.get("occurrences")):
            occurrence = require_object(value_occurrence)
            name = text(occurrence.get("sheet"))
            row = integer(occurrence.get("row"))
            last[name] = max(row, last.get(name, 0))
            occurrences.append(
                ClientOccurrence(
                    location=ClientLocation(sheet=name, row=row),
                    raw_name=text(occurrence.get("raw_name")),
                )
            )
        people.append(
            ClientPerson(
                person_id=text(person.get("id")),
                sorted_name_key=text(person.get("sorted_name_key")),
                canonical_name=text(person.get("canonical_name")),
                holders=tuple(text(holder) for holder in require_list(person.get("holders"))),
                occurrences=tuple(occurrences),
            )
        )
    return ClientCatalog(
        source_id="compatibility:matching-only",
        revision="without-revision-of-source",
        generated_at=datetime(1970, 1, 1, tzinfo=UTC),
        sheets=tuple(
            SheetInfo(
                name=name,
                sheet_id=catalog_index,
                name_column=1,
                first_week_column=None,
                last_week_column=None,
                last_client_row=last,
            )
            for catalog_index, (name, last) in enumerate(sorted(last.items()))
        ),
        people=tuple(people),
    )


def contact_from_legacy(input: object) -> WeeklyContact:
    """Validate contact metadata without inventing missing dates."""
    from datetime import date

    contact = require_object(input)
    date_text = text(contact.get("last_activity_date"))
    try:
        activity_date = date.fromisoformat(date_text)
    except ValueError as cause:
        raise StructureError("The contact date is invalid.") from cause
    if activity_date.isoformat() != date_text:
        raise StructureError("The contact date must use YYYY-MM-DD.")
    group = contact.get("is_group", False)
    if type(group) is not bool:
        raise StructureError("The group flag must be a boolean.")
    phone = contact.get("phone")
    return WeeklyContact(
        raw_name=text(contact.get("raw_name")),
        last_activity_date=activity_date,
        phone=None if phone is None else text(phone),
        is_group=group,
    )


def alias_from_legacy(name: str, input: object) -> ContactAlias:
    """Convert destinations and phone metadata using current domain invariants."""
    document = require_object(input)
    destinations: list[AliasDestination] = []
    for value in require_list(document.get("people")):
        destination = require_object(value)
        sheets = tuple(text(sheet) for sheet in require_list(destination.get("sheets")))
        if len(set(sheets)) != len(sheets):
            raise StructureError("Alias sheets cannot be duplicated.")
        destinations.append(
            AliasDestination(
                person_id=text(destination.get("person_id")),
                sheets=frozenset(sheets),
            )
        )
    phone = document.get("phone")
    return ContactAlias(
        raw_name=name,
        people=tuple(destinations),
        phone=None if phone is None else text(phone),
    )


def alias_to_legacy(alias: ContactAlias) -> LegacyAlias:
    """Return an independent alias copy without exposing mutable store state."""
    return {
        "people": [
            {"person_id": destination.person_id, "sheets": sorted(destination.sheets)}
            for destination in alias.people
        ],
        "phone": alias.phone,
    }


def result_to_legacy(result: ContactResult) -> LegacyResult:
    """Adapt presentation only; all matching decisions belong to the engine."""
    base: LegacyResult = {
        "kind": result.kind,
        "raw_name": result.contact.raw_name,
    }
    if isinstance(result, ResolvedContact):
        first: dict[str, int] = {}
        for location in result.locations:
            first.setdefault(location.sheet, location.row)
        base["person_id"] = result.person_id
        base["sheets"] = sorted(first)
        base["row_by_sheet"] = first
        base["locations"] = [
            {"sheet": location.sheet, "row": location.row} for location in result.locations
        ]
        base["method"] = result.method.value
        base["score"] = result.score
    elif isinstance(result, PendingContact):
        base["reason"] = result.reason.value
        base["candidates"] = [
            {
                "person_id": candidate.person_id,
                "name": candidate.canonical_name,
                "score": candidate.score,
            }
            for candidate in result.candidates
        ]
    elif isinstance(result, IgnoredContact):
        base["reason"] = result.reason.value
    return base
