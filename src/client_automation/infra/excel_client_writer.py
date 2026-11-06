"""XLSX backups and weekly writes guarded by optimistic source revisions."""

import logging
from copy import copy
from datetime import UTC
from hashlib import sha256
from io import BytesIO
from os import fsync
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import cast
from zipfile import BadZipFile, ZipFile

from openpyxl import Workbook, load_workbook
from openpyxl.cell.cell import Cell
from openpyxl.cell.rich_text import CellRichText
from openpyxl.descriptors.serialisable import Serialisable
from openpyxl.styles import PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.utils.exceptions import InvalidFileException
from openpyxl.worksheet.worksheet import Worksheet
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from client_automation.core.contracts import Clock
from client_automation.core.errors import (
    ConcurrencyError,
    ConfigurationError,
    PersistenceError,
    StructureError,
)
from client_automation.core.input_formats import CLIENT_COLUMN_HEADERS
from client_automation.infra.excel_client_reader import (
    EXPECTED_SHEETS,
    FIRST_CLIENT_ROW,
    GREEN_RGB,
    MAX_EXCEL_COLUMN,
    WEEK_HEADER_ROW,
)

LOGGER = logging.getLogger(__name__)


class ExcelClientWriter:
    """Implement ClientWriter without retaining open workbooks or file handles."""

    def __init__(self, path: Path, *, backup_directory: Path, clock: Clock) -> None:
        """Accept paths and a clock without accessing the filesystem."""
        if path.suffix.lower() != ".xlsx":
            raise ConfigurationError("The Excel writer requires an XLSX file.")
        self._path = path
        self._directory_backups = backup_directory
        self._clock = clock

    def create_backup(self) -> Path:
        """Copy the current bytes exactly to an exclusive backup filename."""
        instant = self._clock.now()
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ConfigurationError("The backup clock must be timezone-aware.")
        prefix = instant.astimezone(UTC).strftime("clients_%Y%m%dT%H%M%S_%fZ_")
        content = self._read_content()
        revision = sha256(content).hexdigest()
        backup: Path | None = None
        completed = False
        try:
            self._directory_backups.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(
                mode="wb",
                dir=self._directory_backups,
                prefix=prefix,
                suffix=".xlsx",
                delete=False,
            ) as file:
                backup = Path(file.name)
                file.write(content)
                file.flush()
                fsync(file.fileno())
            self._check_revision(revision)
            completed = True
        except OSError as cause:
            raise PersistenceError("The Excel backup could not be created.") from cause
        finally:
            if backup is not None and not completed:
                _remove_temporary(backup)
        LOGGER.info("Excel backup created and synchronized.")
        return backup

    def apply_marks(
        self,
        sheet: str,
        column: int,
        header: str,
        rows: frozenset[int],
        *,
        expected_revision: str,
    ) -> None:
        """Update an existing week or append the next without inserting columns."""
        _validate_request(sheet, column, header, rows, expected_revision)
        content = self._read_content()
        if sha256(content).hexdigest() != expected_revision:
            raise ConcurrencyError("The Excel workbook changed. Read and plan again.")
        try:
            _validate_components(content)
            with BytesIO(content) as flow:
                workbook = load_workbook(flow, data_only=False, keep_links=True, rich_text=True)
                try:
                    destination, client_rows, new = _validate_destination(
                        workbook,
                        sheet,
                        column,
                        header,
                        rows,
                    )
                    if new:
                        _copy_format_header(destination, column)
                        destination.cell(WEEK_HEADER_ROW, column).value = header.strip()
                    for row in client_rows:
                        cell = destination.cell(row, column)
                        cell.value = None
                        cell.fill = PatternFill(
                            patternType="solid",
                            fgColor="FF" + GREEN_RGB if row in rows else "FFFFFFFF",
                        )
                    self._save(workbook, expected_revision)
                finally:
                    workbook.close()
        except OSError as cause:
            raise PersistenceError("The Excel workbook could not be saved.") from cause
        except (
            BadZipFile,
            InvalidFileException,
            SyntaxError,
            ValueError,
            KeyError,
            IndexError,
            TypeError,
        ) as cause:
            raise StructureError(
                "The XLSX workbook is invalid or does not support this operation."
            ) from cause
        LOGGER.info("Excel week updated: %s clients marked.", len(rows))

    def _read_content(self) -> bytes:
        try:
            return self._path.read_bytes()
        except OSError as cause:
            raise PersistenceError("The Excel client file could not be read.") from cause

    def _check_revision(self, expected_revision: str) -> None:
        if sha256(self._read_content()).hexdigest() != expected_revision:
            raise ConcurrencyError("The Excel workbook changed. Read and plan again.")

    def _save(self, workbook: Workbook, expected_revision: str) -> None:
        temporary: Path | None = None
        try:
            with NamedTemporaryFile(
                mode="w+b",
                dir=self._path.parent,
                prefix=".excel-",
                suffix=".tmp",
                delete=False,
            ) as file:
                temporary = Path(file.name)
                file.write(_serialize(workbook))
                file.flush()
                fsync(file.fileno())
            for attempt in Retrying(
                retry=retry_if_exception(_is_lock_temporary),
                stop=stop_after_attempt(4),
                wait=wait_exponential(multiplier=0.05, min=0.05, max=0.2),
                before_sleep=_warn_retry,
                reraise=True,
            ):
                with attempt:
                    # Recheck the precondition after a temporary file lock as well.
                    self._check_revision(expected_revision)
                    temporary.replace(self._path)
            temporary = None
        finally:
            if temporary is not None:
                _remove_temporary(temporary)


def _validate_request(
    sheet: str,
    column: int,
    header: str,
    rows: frozenset[int],
    revision: str,
) -> None:
    if sheet not in EXPECTED_SHEETS:
        raise StructureError("The requested sheet is not a configured client sheet.")
    if type(column) is not int or not 1 <= column <= MAX_EXCEL_COLUMN:
        raise StructureError("The weekly column must be a valid Excel index.")
    if (
        not header.strip()
        or len(header.strip()) > 32767
        or header.strip().startswith("=")
        or header.strip()
        in {
            "#NULL!",
            "#DIV/0!",
            "#VALUE!",
            "#REF!",
            "#NAME?",
            "#NUM!",
            "#N/A",
        }
        or any(ord(character) < 32 and character not in "\t\n\r" for character in header)
    ):
        raise StructureError("The weekly header must be valid literal text.")
    if any(type(row) is not int or not 3 <= row <= 1048576 for row in rows):
        raise StructureError("Marked rows must be valid client indices.")
    if not revision.strip():
        raise StructureError("Writing requires a source revision.")


def _validate_components(content: bytes) -> None:
    # openpyxl cannot fully preserve these workbook parts when serializing again.
    prefixes = (
        "xl/drawings/",
        "xl/vbaProject",
        "xl/activeX/",
        "xl/ctrlProps/",
        "xl/slicers/",
        "xl/slicerCaches/",
        "_xmlsignatures/",
    )
    with ZipFile(BytesIO(content)) as package:
        if any(name.startswith(prefixes) for name in package.namelist()):
            raise StructureError(
                "The writer supports tabular workbooks without drawings, "
                "macros, controls, or signatures."
            )


def _validate_destination(
    workbook: Workbook,
    name: str,
    column: int,
    header: str,
    rows: frozenset[int],
) -> tuple[Worksheet, frozenset[int], bool]:
    if not set(EXPECTED_SHEETS) <= set(workbook.sheetnames):
        raise StructureError("The workbook lacks required client sheets.")
    sheet = workbook[name]
    if not isinstance(sheet, Worksheet) or sheet.protection.sheet:
        raise StructureError("The destination sheet must be tabular and unprotected.")
    title = sheet.cell(1, 1)
    if (
        not isinstance(title.value, str)
        or title.data_type in {"f", "e"}
        or title.value.strip().casefold() not in CLIENT_COLUMN_HEADERS
    ):
        raise StructureError("Column A must have a valid client header.")
    first = 4 if name == "Balanz" else 3
    weeks: dict[int, str] = {}
    for catalog_index in range(first, sheet.max_column + 1):
        cell = sheet.cell(WEEK_HEADER_ROW, catalog_index)
        if cell.value is not None and str(cell.value).strip():
            if cell.data_type in {"f", "e"}:
                raise StructureError("Weekly headers must be literal values.")
            weeks[catalog_index] = str(cell.value).strip()
    matching = [catalog_index for catalog_index, text in weeks.items() if text == header.strip()]
    new = not matching
    next = max(weeks) + 1 if weeks else first
    if (not new and matching != [column]) or (new and column != next):
        raise StructureError("The column is not the existing or next weekly column.")
    if any(period.min_col <= column <= period.max_col for period in sheet.merged_cells.ranges):
        raise StructureError("The weekly column cannot contain merged cells.")
    clients: set[int] = set()
    for catalog_index in range(FIRST_CLIENT_ROW, sheet.max_row + 1):
        cell_name = sheet.cell(catalog_index, 1)
        value = cell_name.value
        if value is None or (isinstance(value, (str, CellRichText)) and not str(value).strip()):
            continue
        if not isinstance(value, (str, CellRichText)) or cell_name.data_type in {"f", "e"}:
            raise StructureError("Client names must be literal text.")
        cell = sheet.cell(catalog_index, column)
        if cell.data_type in {"f", "e"} or (
            cell.value is not None
            and not (isinstance(cell.value, str) and cell.value.strip().casefold() in {"", "x"})
        ):
            raise StructureError(
                "The weekly column contains formulas or values unrelated to marks."
            )
        clients.add(catalog_index)
    if not rows <= clients:
        raise StructureError("The selection includes rows without clients in the current source.")
    return sheet, frozenset(clients), new


def _copy_format_header(sheet: Worksheet, column: int) -> None:
    first = 4 if sheet.title == "Balanz" else 3
    if column <= first:
        return
    source = sheet.cell(WEEK_HEADER_ROW, column - 1)
    destination = sheet.cell(WEEK_HEADER_ROW, column)
    if not isinstance(source, Cell) or not isinstance(destination, Cell):
        raise StructureError("The weekly header cannot be merged.")
    # StyleProxy.__copy__ returns the actual style, not another proxy; its stubs are imprecise.
    destination.font = cast(Serialisable, copy(source.font))
    destination.fill = cast(Serialisable, copy(source.fill))
    destination.border = cast(Serialisable, copy(source.border))
    destination.alignment = cast(Serialisable, copy(source.alignment))
    destination.protection = cast(Serialisable, copy(source.protection))
    destination.number_format = source.number_format
    letter_previous = get_column_letter(column - 1)
    if letter_previous in sheet.column_dimensions:
        sheet.column_dimensions[get_column_letter(column)].width = sheet.column_dimensions[
            letter_previous
        ].width


def _serialize(workbook: Workbook) -> bytes:
    with BytesIO() as flow:
        workbook.save(flow)
        return flow.getvalue()


def _is_lock_temporary(cause: BaseException) -> bool:
    return isinstance(cause, OSError) and getattr(cause, "winerror", None) in {32, 33}


def _remove_temporary(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        LOGGER.warning("A temporary Excel file could not be removed after failure.")


def _warn_retry(state: RetryCallState) -> None:
    LOGGER.warning(
        "Excel replacement temporarily locked; retrying after attempt %s.",
        state.attempt_number,
    )
