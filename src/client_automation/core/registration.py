"""Pure weekly mark planning and evidence validation without external dependencies."""

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from client_automation.core.errors import DomainValidationError
from client_automation.core.models import (
    ClientCatalog,
    ClientRow,
    ContactResult,
    WeeklyContacts,
    WeeklyResolution,
)

type Headers = tuple[tuple[int, str], ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class SheetMarks:
    """Complete desired weekly marks for one sheet, including unmarked client rows."""

    sheet: str
    column: int
    header: str
    rows: frozenset[int]
    new_week: bool

    def __post_init__(self) -> None:
        """Require explicit indices and text without type coercion."""
        if not self.sheet.strip() or not self.header.strip():
            raise DomainValidationError("The operation requires a sheet and header.")
        if type(self.column) is not int or self.column < 1:
            raise DomainValidationError("The registration column must be a positive integer.")
        if any(type(row) is not int or row < 1 for row in self.rows):
            raise DomainValidationError("Registration rows must be positive integers.")


@dataclass(frozen=True, slots=True, kw_only=True)
class RegistrationPlan:
    """Immutable registration plan tied to the original source revision."""

    catalog: ClientCatalog = field(repr=False)
    week: WeeklyContacts = field(repr=False)
    resolution: WeeklyResolution = field(repr=False)
    header: str
    allow_pending: bool
    operations: tuple[SheetMarks, ...]
    headers_previous: tuple[Headers, ...] = field(repr=False)
    marks_previous: tuple[frozenset[int], ...] = field(repr=False)

    def __post_init__(self) -> None:
        """Keep operations, sheets, and preconditions aligned."""
        names = tuple(sheet.name for sheet in self.catalog.sheets)
        if not names or tuple(operation.sheet for operation in self.operations) != names:
            raise DomainValidationError("The plan must contain one operation per catalog sheet.")
        if not len(names) == len(self.headers_previous) == len(self.marks_previous):
            raise DomainValidationError("Plan preconditions must cover every sheet.")


@dataclass(frozen=True, slots=True, kw_only=True)
class RegistrationResult:
    """Verifiable outcome; a dry run has no backup or applied sheets."""

    plan: RegistrationPlan = field(repr=False)
    dry_run: bool
    backup: Path | str | None = field(repr=False)
    applied_sheets: tuple[str, ...]
    revision_final: str

    def __post_init__(self) -> None:
        """Reject dry-run side effects and writes without a backup."""
        if not self.revision_final.strip():
            raise DomainValidationError("The result requires a final revision.")
        if self.dry_run and (self.backup is not None or self.applied_sheets):
            raise DomainValidationError("A dry run cannot have write side effects.")
        if self.applied_sheets and self.backup is None:
            raise DomainValidationError("Writes require a prior backup.")
        if len(set(self.applied_sheets)) != len(self.applied_sheets) or not set(
            self.applied_sheets
        ) <= {operation.sheet for operation in self.plan.operations}:
            raise DomainValidationError("Applied sheets must be unique and belong to the plan.")


def catalog_clients(catalog: ClientCatalog) -> dict[str, tuple[ClientRow, ...]]:
    """Reconstruct original client cells and deduplicate joint-holder rows."""
    cells: dict[str, dict[int, str]] = {sheet.name: {} for sheet in catalog.sheets}
    for person in catalog.people:
        for occurrence in person.occurrences:
            location = occurrence.location
            previous = cells[location.sheet]
            if location.row in previous and previous[location.row] != occurrence.raw_name:
                raise DomainValidationError("The catalog assigns different text to the same cell.")
            previous[location.row] = occurrence.raw_name
    return {
        name: tuple(ClientRow(row=row, raw_name=text) for row, text in sorted(rows.items()))
        for name, rows in cells.items()
    }


def build_operations(
    *,
    catalog: ClientCatalog,
    week: WeeklyContacts,
    resolution: WeeklyResolution,
    header: str,
    headers: Mapping[str, Headers],
    first_columns: Mapping[str, int],
    allow_pending: bool = False,
) -> tuple[SheetMarks, ...]:
    """Group complete destinations and select an existing or next weekly column."""
    if not header.strip() or header != header.strip():
        raise DomainValidationError("Use a canonical weekly header without surrounding whitespace.")
    if resolution.pending and not allow_pending:
        raise DomainValidationError(
            "Review pending contacts before planning the weekly replacement."
        )
    results: tuple[ContactResult, ...] = (
        *resolution.resolved,
        *resolution.pending,
        *resolution.ignored,
    )
    contacts = Counter(week.contacts)
    present = Counter(result.contact for result in results)
    if set(contacts) != set(present) or any(
        present[contact] < count for contact, count in contacts.items()
    ):
        raise DomainValidationError("The resolution must cover the supplied weekly contacts.")
    if not catalog.sheets or set(headers) != {sheet.name for sheet in catalog.sheets}:
        raise DomainValidationError("Headers are required for every catalog sheet.")
    people = {person.person_id: person for person in catalog.people}
    rows: dict[str, set[int]] = {sheet.name: set() for sheet in catalog.sheets}
    for resolved in resolution.resolved:
        person = people.get(resolved.person_id)
        if person is None or resolved.contact.is_group:
            raise DomainValidationError(
                "The resolved result does not identify a registrable person."
            )
        sheets_selected = {location.sheet for location in resolved.locations}
        expected = {location for location in person.locations if location.sheet in sheets_selected}
        if set(resolved.locations) != expected:
            raise DomainValidationError(
                "The result must preserve every row of the selected sheets."
            )
        for location in resolved.locations:
            rows[location.sheet].add(location.row)
    operations: list[SheetMarks] = []
    for sheet in catalog.sheets:
        first = first_columns.get(sheet.name)
        if first is None or type(first) is not int or first <= sheet.name_column:
            raise DomainValidationError("Configure the first weekly column for every sheet.")
        existing = headers[sheet.name]
        columns = tuple(column for column, _ in existing)
        if (
            columns != tuple(sorted(set(columns)))
            or any(
                type(column) is not int or column < first or not text.strip()
                for column, text in existing
            )
            or (columns[0] if columns else None) != sheet.first_week_column
            or (columns[-1] if columns else None) != sheet.last_week_column
        ):
            raise DomainValidationError("Headers do not correspond to the catalog structure.")
        matching = tuple(column for column, text in existing if text.strip() == header)
        if len(matching) > 1:
            raise DomainValidationError("The requested header occurs in more than one column.")
        column = matching[0] if matching else (columns[-1] + 1 if columns else first)
        operations.append(
            SheetMarks(
                sheet=sheet.name,
                column=column,
                header=header,
                rows=frozenset(rows[sheet.name]),
                new_week=not matching,
            )
        )
    return tuple(operations)
