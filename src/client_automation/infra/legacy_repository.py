"""Legacy list and set facade delegating to the current adapters."""

import logging
from pathlib import Path
from types import TracebackType
from typing import Self

from client_automation.core.contracts import ClientWriter
from client_automation.core.errors import ConfigurationError, InfrastructureError
from client_automation.core.models import ClientRow, SheetInfo
from client_automation.infra.excel_client_reader import ExcelClientReader
from client_automation.infra.sheets_client_reader import SheetsClientReader

LOGGER = logging.getLogger(__name__)


class LegacyRepository:
    """Adapt collections while retaining all write preconditions."""

    def __init__(self, reader: ExcelClientReader | SheetsClientReader) -> None:
        """Accept an existing reader and manage its lifecycle."""
        self.reader = reader
        self._closed = False

    def list_sheets(self) -> list[SheetInfo]:
        """Adapt validated sheet structure to a list."""
        return list(self.reader.list_sheets())

    def read_clients(self, sheet: str) -> list[ClientRow]:
        """Preserve every client row and original name."""
        return list(self.reader.read_clients(sheet))

    def last_week(self, sheet: str) -> tuple[int, str]:
        """Query the current reader for its last weekly header."""
        return self.reader.last_week(sheet)

    def read_marks(self, sheet: str, col: int) -> set[int]:
        """Return an independent mutable set of marked rows."""
        return set(self.reader.read_marks(sheet, col))

    def revision(self) -> str:
        """Get a fresh revision including values and formatting."""
        return self.reader.revision()

    def get_writer(self) -> ClientWriter:
        """Provide the current writer for the same source."""
        raise NotImplementedError

    def check_open(self) -> None:
        """Prevent resource creation after the facade has been closed."""
        if self._closed:
            raise ConfigurationError("The repository is closed. Create a new session.")

    def create_backup(self) -> Path | str:
        """Delegate backup creation to the current writer."""
        return self.get_writer().create_backup()

    def apply_marks(
        self,
        sheet: str,
        col: int,
        header: str,
        rows: set[int],
        *,
        expected_revision: str | None = None,
    ) -> None:
        """Require an explicit revision; never silently refresh it to permit a write."""
        if expected_revision is None:
            raise ConfigurationError("The legacy write API requires expected_revision.")
        self.get_writer().apply_marks(
            sheet, col, header, frozenset(rows), expected_revision=expected_revision
        )

    def close(self) -> None:
        """Release and invalidate the reader."""
        if not self._closed:
            self._closed = True
            self.reader.close()

    def __enter__(self) -> Self:
        """Release resources through a context manager."""
        self.check_open()
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Preserve the block's original exception if closing also fails."""
        try:
            self.close()
        except InfrastructureError:
            if error is None:
                raise
            LOGGER.warning("A legacy adapter could not be closed after failure.")
