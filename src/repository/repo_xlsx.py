"""Legacy XLSX facade backed by the current adapters."""

from pathlib import Path

from client_automation.core.contracts import ClientWriter
from client_automation.infra.excel_client_reader import ExcelClientReader
from client_automation.infra.excel_client_writer import ExcelClientWriter
from client_automation.infra.legacy_repository import LegacyRepository
from client_automation.infra.system_clock import SystemClock


class RepoXlsx(LegacyRepository):
    """Preserve legacy queries while requiring explicit revisions for writes."""

    def __init__(self, path: str | Path, *, backup_directory: Path | None = None) -> None:
        """Construct adapters without opening the workbook."""
        self.path = Path(path)
        self._writer = ExcelClientWriter(
            self.path,
            backup_directory=backup_directory or self.path.parent / "backups",
            clock=SystemClock(),
        )
        super().__init__(ExcelClientReader(self.path))

    def get_writer(self) -> ClientWriter:
        """Return the same XLSX writer throughout the session."""
        self.check_open()
        return self._writer
