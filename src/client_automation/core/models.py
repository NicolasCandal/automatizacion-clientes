"""Immutable weekly registration models using only the standard library."""

from __future__ import annotations

from collections.abc import Hashable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from math import isfinite
from typing import Literal

from .errors import DomainValidationError


class MatchMethod(StrEnum):
    """Evidence used to identify a client person."""

    PHONE = "phone"
    ALIAS = "alias"
    EXACT = "exact"
    FUZZY = "fuzzy"


class PendingReason(StrEnum):
    """Reasons a contact requires human review."""

    AMBIGUOUS = "ambiguous"
    NO_MATCH = "no_match"
    BROKER_MISMATCH = "broker_mismatch"


class IgnoredReason(StrEnum):
    """Reasons a contact is explicitly excluded from weekly registration."""

    GROUP = "group"
    EXCLUDED = "excluded"


def _validate_text(value: str, name: str) -> None:
    """Require meaningful text without modifying or exposing the original value."""
    if not value.strip():
        raise DomainValidationError(f"{name} cannot be empty.")


def _validate_integer(value: int, name: str, minimum: int = 1) -> None:
    """Require valid integer indices and reject Python booleans."""
    if type(value) is not int or value < minimum:
        raise DomainValidationError(
            f"{name} must be an integer greater than or equal to {minimum}."
        )


def _validate_unique(values: Iterable[Hashable], name: str) -> None:
    """Detect duplicate identifiers or locations without discarding records."""
    elements = tuple(values)
    if len(elements) != len(set(elements)):
        raise DomainValidationError(f"{name} cannot contain duplicates.")


def _validate_score(score: float) -> None:
    """Require a finite score in the inclusive range from zero to 100."""
    if isinstance(score, bool) or not isfinite(score) or not 0 <= score <= 100:
        raise DomainValidationError("The score must be a finite number between 0 and 100.")


@dataclass(frozen=True, slots=True, kw_only=True)
class WeeklyPeriod:
    """Inclusive calendar-date period, including a week still in progress."""

    start_date: date
    end_date: date

    def __post_init__(self) -> None:
        """Reject timestamps and inverted calendar-date ranges."""
        if type(self.start_date) is not date or type(self.end_date) is not date:
            raise DomainValidationError("The period must use calendar dates without times.")
        if self.end_date < self.start_date:
            raise DomainValidationError("The end date cannot precede the start date.")

    def contains(self, calendar_date: date) -> bool:
        """Return whether a calendar date is within the inclusive period."""
        if type(calendar_date) is not date:
            raise DomainValidationError("The queried date must not include a time.")
        return self.start_date <= calendar_date <= self.end_date


@dataclass(frozen=True, slots=True, kw_only=True)
class SheetInfo:
    """One-based sheet structure; sheet IDs may be zero and weeks may be absent."""

    name: str
    sheet_id: int
    name_column: int
    first_week_column: int | None
    last_week_column: int | None
    last_client_row: int

    def __post_init__(self) -> None:
        """Require consistent indices and weekly column bounds."""
        _validate_text(self.name, "The sheet name")
        _validate_integer(self.sheet_id, "sheet_id", minimum=0)
        _validate_integer(self.name_column, "The name column")
        _validate_integer(self.last_client_row, "The last row", minimum=0)
        first = self.first_week_column
        last = self.last_week_column
        if (first is None) != (last is None):
            raise DomainValidationError(
                "Both weekly column bounds must be defined or both must be absent."
            )
        if first is not None and last is not None:
            _validate_integer(first, "The first weekly column")
            _validate_integer(last, "The last weekly column")
            if first <= self.name_column or last < first:
                raise DomainValidationError(
                    "Weekly columns must follow the name column and have ordered bounds."
                )


@dataclass(frozen=True, slots=True, kw_only=True)
class ClientRow:
    """Source row preserving the client's original name."""

    row: int
    raw_name: str = field(repr=False)

    def __post_init__(self) -> None:
        """Reject invalid rows and cells without a usable name."""
        _validate_integer(self.row, "The client row")
        _validate_text(self.raw_name, "The original name")


@dataclass(frozen=True, slots=True, kw_only=True)
class ClientLocation:
    """Reference to a client row within a sheet."""

    sheet: str
    row: int

    def __post_init__(self) -> None:
        """Validate the reference without querying any spreadsheet."""
        _validate_text(self.sheet, "The client sheet")
        _validate_integer(self.row, "The client row")


@dataclass(frozen=True, slots=True, kw_only=True)
class ClientOccurrence:
    """Association between a row location and its original cell text."""

    location: ClientLocation
    raw_name: str = field(repr=False)

    def __post_init__(self) -> None:
        """Preserve the original text, including joint holders' names."""
        _validate_text(self.raw_name, "The original name")


@dataclass(frozen=True, slots=True, kw_only=True)
class ClientPerson:
    """Identified person who may occur in several rows or sheets."""

    person_id: str
    sorted_name_key: str = field(repr=False)
    canonical_name: str = field(repr=False)
    holders: tuple[str, ...] = field(repr=False)
    occurrences: tuple[ClientOccurrence, ...] = field(repr=False)

    def __post_init__(self) -> None:
        """Require consistent identities and occurrences without normalizing names."""
        _validate_text(self.person_id, "The person identifier")
        _validate_text(self.sorted_name_key, "The sorted name key")
        _validate_text(self.canonical_name, "The canonical name")
        if not self.holders or not self.occurrences:
            raise DomainValidationError("A person must have holders and occurrences.")
        for holder in self.holders:
            _validate_text(holder, "The holder")
        _validate_unique(self.holders, "The holders")
        _validate_unique(self.locations, "The person's locations")

    @property
    def locations(self) -> tuple[ClientLocation, ...]:
        """Return every location, including multiple rows within the same sheet."""
        return tuple(occurrence.location for occurrence in self.occurrences)

    @property
    def sheets(self) -> frozenset[str]:
        """Return the person's sheet names without duplicates."""
        return frozenset(location.sheet for location in self.locations)


@dataclass(frozen=True, slots=True, kw_only=True)
class ClientCatalog:
    """Client snapshot tied to an explicit source identity and revision."""

    source_id: str = field(repr=False)
    revision: str
    generated_at: datetime
    sheets: tuple[SheetInfo, ...]
    people: tuple[ClientPerson, ...] = field(repr=False)

    def __post_init__(self) -> None:
        """Validate identities, references, row bounds, and timezone awareness."""
        _validate_text(self.source_id, "The catalog source")
        _validate_text(self.revision, "The catalog revision")
        if self.generated_at.utcoffset() is None:
            raise DomainValidationError("The catalog generation timestamp must be timezone-aware.")
        _validate_unique((sheet.name for sheet in self.sheets), "The sheet names")
        _validate_unique((sheet.sheet_id for sheet in self.sheets), "The sheet identifiers")
        _validate_unique((person.person_id for person in self.people), "The people")
        _validate_unique((person.sorted_name_key for person in self.people), "The sorted name keys")
        sheet_names = frozenset(sheet.name for sheet in self.sheets)
        last_rows = {sheet.name: sheet.last_client_row for sheet in self.sheets}
        for person in self.people:
            if not person.sheets <= sheet_names:
                raise DomainValidationError("A person references a sheet absent from the catalog.")
            if any(location.row > last_rows[location.sheet] for location in person.locations):
                raise DomainValidationError(
                    "A client location exceeds the sheet's declared last row."
                )


@dataclass(frozen=True, slots=True, kw_only=True)
class WeeklyContact:
    """Chat metadata without retaining message bodies."""

    raw_name: str = field(repr=False)
    last_activity_date: date
    phone: str | None = field(default=None, repr=False)
    is_group: bool = False

    def __post_init__(self) -> None:
        """Require a calendar date and at least one available chat identity."""
        if type(self.last_activity_date) is not date:
            raise DomainValidationError("The contact's last activity date must not include a time.")
        if self.phone is not None:
            _validate_text(self.phone, "The phone number")
        if not self.raw_name.strip() and self.phone is None:
            raise DomainValidationError("A contact must have a name or phone number.")


@dataclass(frozen=True, slots=True, kw_only=True)
class WeeklyContacts:
    """Contacts extracted within a specified inclusive calendar period."""

    period: WeeklyPeriod
    contacts: tuple[WeeklyContact, ...] = field(repr=False)

    def __post_init__(self) -> None:
        """Reject weekly contacts whose dates fall outside the period."""
        if any(not self.period.contains(contact.last_activity_date) for contact in self.contacts):
            raise DomainValidationError("All contacts must belong to the reported period.")


@dataclass(frozen=True, slots=True, kw_only=True)
class MatchCandidate:
    """Proposed person without a registration decision."""

    person_id: str
    canonical_name: str = field(repr=False)
    sheets: frozenset[str]
    score: float
    method: MatchMethod

    def __post_init__(self) -> None:
        """Validate identity, selected sheets, and the scoring scale."""
        _validate_text(self.person_id, "The candidate identifier")
        _validate_text(self.canonical_name, "The candidate name")
        _validate_score(self.score)
        if not self.sheets:
            raise DomainValidationError("A candidate must occur in at least one sheet.")
        for sheet in self.sheets:
            _validate_text(sheet, "The candidate sheet")


@dataclass(frozen=True, slots=True, kw_only=True)
class ResolvedContact:
    """Resolved person and every concrete row to be marked."""

    contact: WeeklyContact = field(repr=False)
    person_id: str
    locations: tuple[ClientLocation, ...]
    score: float
    method: MatchMethod
    kind: Literal["resolved"] = field(default="resolved", init=False)

    def __post_init__(self) -> None:
        """Reject assignments without destinations or with duplicate rows."""
        _validate_text(self.person_id, "The person identifier")
        _validate_score(self.score)
        if not self.locations:
            raise DomainValidationError(
                "A resolved contact must have at least one destination location."
            )
        _validate_unique(self.locations, "The destination locations")


@dataclass(frozen=True, slots=True, kw_only=True)
class PendingContact:
    """Contact requiring review, without writable destinations."""

    contact: WeeklyContact = field(repr=False)
    reason: PendingReason
    candidates: tuple[MatchCandidate, ...] = field(default=(), repr=False)
    kind: Literal["pending"] = field(default="pending", init=False)

    def __post_init__(self) -> None:
        """Reject duplicate candidates representing the same person."""
        _validate_unique((candidate.person_id for candidate in self.candidates), "The candidates")


@dataclass(frozen=True, slots=True, kw_only=True)
class IgnoredContact:
    """Contact excluded by an explicit business rule."""

    contact: WeeklyContact = field(repr=False)
    reason: IgnoredReason
    kind: Literal["ignored"] = field(default="ignored", init=False)


type ContactResult = ResolvedContact | PendingContact | IgnoredContact


@dataclass(frozen=True, slots=True, kw_only=True)
class WeeklyResolution:
    """Grouped results; a single contact may resolve to several holders."""

    resolved: tuple[ResolvedContact, ...] = field(default=(), repr=False)
    pending: tuple[PendingContact, ...] = field(default=(), repr=False)
    ignored: tuple[IgnoredContact, ...] = field(default=(), repr=False)
    warnings: tuple[str, ...] = field(default=(), repr=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class AliasDestination:
    """Learned person and selected sheets without potentially stale row indices."""

    person_id: str
    sheets: frozenset[str]

    def __post_init__(self) -> None:
        """Validate learned destinations and reject duplicate selected sheets."""
        _validate_text(self.person_id, "The alias person identifier")
        if not self.sheets:
            raise DomainValidationError("An alias destination must have at least one sheet.")
        for sheet in self.sheets:
            _validate_text(sheet, "The alias sheet")


@dataclass(frozen=True, slots=True, kw_only=True)
class ContactAlias:
    """Learned contact association, including possible joint holders."""

    raw_name: str = field(repr=False)
    people: tuple[AliasDestination, ...] = field(repr=False)
    phone: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Validate learned contact identity and holder associations."""
        if self.phone is not None:
            _validate_text(self.phone, "The alias phone number")
        if not self.raw_name.strip() and self.phone is None:
            raise DomainValidationError("An alias must have a name or phone number.")
        if not self.people:
            raise DomainValidationError("An alias must point to at least one person.")
        _validate_unique((person.person_id for person in self.people), "The alias people")


@dataclass(frozen=True, slots=True, kw_only=True)
class NameKeys:
    """Original text and keys derived by the normalization functions."""

    raw: str = field(repr=False)
    normalized: str = field(repr=False)
    sorted_name_key: str = field(repr=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class BrokerName:
    """Broker recognition result without a matching decision."""

    name_without_parentheses: str = field(repr=False)
    sheets: tuple[str, ...]
    noise: tuple[str, ...] = field(repr=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class MatchingPolicy:
    """Acceptance thresholds and the human-review suggestion limit."""

    automatic_threshold: float = 92.0
    review_threshold: float = 80.0
    suggestion_limit: int = 3

    def __post_init__(self) -> None:
        """Validate score thresholds and their ordering without reading settings."""
        _validate_score(self.automatic_threshold)
        _validate_score(self.review_threshold)
        _validate_integer(self.suggestion_limit, "The suggestion limit")
        if self.review_threshold >= self.automatic_threshold:
            raise DomainValidationError(
                "The review threshold must be lower than the automatic threshold."
            )
