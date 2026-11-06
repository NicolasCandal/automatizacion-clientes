"""Sheets facade without a Drive client or duplicate HTTP implementation."""

from google.auth.credentials import Credentials

from client_automation.config import Config
from client_automation.core.contracts import ClientWriter
from client_automation.core.errors import ConfigurationError
from client_automation.infra.legacy_repository import LegacyRepository
from client_automation.infra.sheets_client_reader import create_reader_sheets
from client_automation.infra.sheets_client_writer import (
    SheetsClientWriter,
    create_writer_sheets,
)
from client_automation.infra.system_clock import SystemClock


class RepoSheets(LegacyRepository):
    """Delegate reads, revisions, and backups; require explicit write permissions."""

    def __init__(
        self, spreadsheet_id: str, credentials: Credentials, *, configuration: Config | None = None
    ) -> None:
        """Create a reader session without network access or credential-file reads."""
        self._config = configuration or Config(
            repository="sheets", spreadsheet_id=spreadsheet_id, _env_file=None
        )
        if self._config.spreadsheet_id != spreadsheet_id:
            raise ConfigurationError("The configuration must refer to the requested document.")
        self._credentials = credentials
        self._writer: SheetsClientWriter | None = None
        super().__init__(create_reader_sheets(self._config, credentials))

    def get_writer(self) -> ClientWriter:
        """Create the writer only when an operation requires it."""
        self.check_open()
        if self._writer is None:
            self._writer = create_writer_sheets(
                self._config, self._credentials, clock=SystemClock()
            )
        return self._writer

    def close(self) -> None:
        """Close both sessions even if one close operation fails."""
        if self._closed:
            return
        try:
            if self._writer is not None:
                self._writer.close()
        finally:
            super().close()
