"""Typed structural ports connecting the domain to external adapters."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Protocol

from .models import (
    ClientCatalog,
    ClientRow,
    ContactAlias,
    SheetInfo,
    WeeklyContacts,
    WeeklyPeriod,
    WeeklyResolution,
)
from .whatsapp_import import ContactExtraction


class Clock(Protocol):
    """Supply timezone-aware instants for reproducible service results."""

    def now(self) -> datetime:
        """Return a timezone-aware instant rather than a calendar date."""
        ...


class ClientReader(Protocol):
    """Read a client spreadsheet without exposing write operations."""

    def list_sheets(self) -> tuple[SheetInfo, ...]:
        """Return configured sheets and their structure in stable order."""
        ...

    def read_clients(self, sheet: str) -> tuple[ClientRow, ...]:
        """Return client rows in source order, preserving original names."""
        ...

    def read_marks(self, sheet: str, column: int) -> frozenset[int]:
        """Return rows marked by color or the legacy text marker."""
        ...

    def last_week(self, sheet: str) -> tuple[int, str]:
        """Return the last column and original header without interpreting dates."""
        ...

    def revision(self) -> str:
        """Return an opaque source revision including marks and formatting."""
        ...


class RegistrationClientReader(ClientReader, Protocol):
    """Read the extra information needed to plan existing or new weeks."""

    def read_headers(self, sheet: str) -> tuple[tuple[int, str], ...]:
        """Return all weekly columns and their original header text in order."""
        ...


class ClientWriter(Protocol):
    """Write through a port; the service controls backups and dry runs."""

    def create_backup(self) -> Path | str:
        """Copy the current state without modifying it and return the backup path."""
        ...

    def apply_marks(
        self,
        sheet: str,
        column: int,
        header: str,
        rows: frozenset[int],
        *,
        expected_revision: str,
    ) -> None:
        """Update one week after checking the expected source revision."""
        ...


class ClientRepository(ClientReader, ClientWriter, Protocol):
    """Complete repository contract composed at the application entry point."""

    def close(self) -> None:
        """Release workbooks, files, and connections; repeated calls are allowed."""
        ...


class ContactReader(Protocol):
    """Read weekly contacts from a source configured at construction time."""

    def read(self) -> WeeklyContacts:
        """Return validated contacts and their inclusive period."""
        ...


class AliasStorePort(Protocol):
    """Query learned identities without invoking the name comparator."""

    def find_by_name(self, raw_name: str) -> ContactAlias | None:
        """Find an exact original name or return None when no alias exists."""
        ...

    def find_by_phone(self, phone: str) -> tuple[ContactAlias, ...]:
        """Return all aliases for a phone number, or an empty tuple."""
        ...

    def register(self, alias: ContactAlias) -> str | None:
        """Add a learned association in memory without overwriting conflicts."""
        ...

    def save(self) -> None:
        """Persist accepted entries or raise PersistenceError."""
        ...


class ExclusionListPort(Protocol):
    """Track contacts explicitly excluded from weekly client registration."""

    def contains(self, raw_name: str) -> bool:
        """Check membership using the domain's name normalization rules."""
        ...

    def add(self, raw_name: str) -> None:
        """Add and persist an exclusion without duplicating existing entries."""
        ...


class NameComparator(Protocol):
    """Compare names numerically without acceptance decisions or broker rules."""

    def similarity(self, query_name: str, candidate_name: str) -> float:
        """Compare normalized names and return a score between zero and 100."""
        ...


class CatalogStore(Protocol):
    """Persist the snapshot used as the client catalog cache."""

    def read(self) -> ClientCatalog | None:
        """Return a validated catalog or None when the file is absent."""
        ...

    def save(self, catalog: ClientCatalog) -> None:
        """Save the snapshot atomically or raise PersistenceError."""
        ...


class ResolutionStore(Protocol):
    """Persist matching reports independently of the spreadsheet."""

    def read(self) -> WeeklyResolution:
        """Load a validated report; a missing file raises PersistenceError."""
        ...

    def save(self, resolution: WeeklyResolution) -> None:
        """Save the report atomically or raise PersistenceError."""
        ...


class ContactExtractor(Protocol):
    """Extract a complete chat batch for an explicit inclusive period."""

    def extract(self, period: WeeklyPeriod) -> ContactExtraction:
        """Validate all inputs before returning the weekly contacts."""
        ...


class WeeklyContactsStore(Protocol):
    """Store the weekly document consumed by the matching workflow."""

    def save(self, extraction: ContactExtraction, *, extracted_at: datetime) -> None:
        """Save a complete extraction with its timezone-aware extraction timestamp."""
        ...
