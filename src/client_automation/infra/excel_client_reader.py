"""Typed XLSX snapshots without modifications to the original workbook."""

import logging
from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from types import TracebackType
from typing import Final, Self
from zipfile import BadZipFile

from openpyxl import load_workbook
from openpyxl.cell.read_only import EmptyCell, ReadOnlyCell
from openpyxl.styles import PatternFill
from openpyxl.utils.exceptions import InvalidFileException
from openpyxl.worksheet._read_only import ReadOnlyWorksheet

from client_automation.core.errors import (
    ConfigurationError,
    PersistenceError,
    StructureError,
)
from client_automation.core.input_formats import CLIENT_COLUMN_HEADERS
from client_automation.core.models import ClientRow, SheetInfo

LOGGER = logging.getLogger(__name__)
EXPECTED_SHEETS: Final[tuple[str, ...]] = ("Inviu", "IOL", "Bull", "Balanz", "PPI")
WEEK_HEADER_ROW: Final[int] = 2
FIRST_CLIENT_ROW: Final[int] = 3
GREEN_RGB: Final[str] = "76923C"
MAX_EXCEL_COLUMN: Final[int] = 16384


@dataclass(frozen=True, slots=True)
class _ReadSheet:
    info: SheetInfo
    clients: tuple[ClientRow, ...]
    last_header: str
    headers: tuple[tuple[int, str], ...]
    marks: dict[int, frozenset[int]]
    columns_color_no_supported: frozenset[int]


class ExcelClientReader:
    """Implement ClientReader for the five required broker sheets."""

    def __init__(self, path: Path) -> None:
        """Configure the path without opening files or creating directories."""
        if path.suffix.lower() != ".xlsx":
            raise ConfigurationError("The Excel reader requires an XLSX file.")
        self._path = path
        self._closed = False
        self._revision_cache: str | None = None
        self._sheets_cache: dict[str, _ReadSheet] = {}

    def list_sheets(self) -> tuple[SheetInfo, ...]:
        """Return required sheets with their current workbook indices."""
        return tuple(sheet.info for sheet in self._get_sheets().values())

    def read_clients(self, sheet: str) -> tuple[ClientRow, ...]:
        """Return original names and all client rows, including rows after gaps."""
        return self._get_sheet(sheet).clients

    def read_marks(self, sheet: str, column: int) -> frozenset[int]:
        """Return client marks only within existing weekly columns."""
        self._ensure_open()
        if type(column) is not int or not 1 <= column <= MAX_EXCEL_COLUMN:
            raise StructureError("The mark column must be a valid Excel index.")
        read = self._get_sheet(sheet)
        first = read.info.first_week_column
        last = read.info.last_week_column
        if first is None or last is None or not first <= column <= last:
            raise StructureError("The requested column does not belong to existing weeks.")
        if column in read.columns_color_no_supported:
            raise StructureError("Convert weekly theme or indexed fills to RGB colors.")
        return read.marks.get(column, frozenset())

    def last_week(self, sheet: str) -> tuple[int, str]:
        """Return the original last header, or column A when no weeks exist."""
        read = self._get_sheet(sheet)
        column = read.info.last_week_column
        return (1, "") if column is None else (column, read.last_header)

    def revision(self) -> str:
        """Hash the current bytes with SHA-256, including styles and formulas."""
        return sha256(self._read_content()).hexdigest()

    def read_headers(self, sheet: str) -> tuple[tuple[int, str], ...]:
        """Return all weekly headers without losing columns or whitespace."""
        return self._get_sheet(sheet).headers

    def close(self) -> None:
        """Release the snapshot and prevent further reads; repeated close is allowed."""
        self._sheets_cache.clear()
        self._revision_cache = None
        self._closed = True

    def __enter__(self) -> Self:
        """Manage snapshot lifetime with a context manager."""
        self._ensure_open()
        return self

    def __exit__(
        self,
        kind_error: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the snapshot without hiding an exception raised inside the block."""
        self.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise ConfigurationError("The Excel reader is closed. Create a new instance.")

    def _read_content(self) -> bytes:
        self._ensure_open()
        try:
            return self._path.read_bytes()
        except OSError as cause:
            raise PersistenceError("The Excel client file could not be read.") from cause

    def _get_sheet(self, name: str) -> _ReadSheet:
        sheets = self._get_sheets()
        if name not in sheets:
            raise StructureError("The requested sheet is not a configured client sheet.")
        return sheets[name]

    def _get_sheets(self) -> dict[str, _ReadSheet]:
        content = self._read_content()
        revision = sha256(content).hexdigest()
        if revision != self._revision_cache:
            try:
                sheets = _parse_workbook(content)
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
                    "The file does not contain a valid, supported XLSX workbook."
                ) from cause
            self._sheets_cache = sheets
            self._revision_cache = revision
            LOGGER.debug("Excel snapshot validated: %s sheets.", len(sheets))
        return self._sheets_cache


def _parse_workbook(content: bytes) -> dict[str, _ReadSheet]:
    with BytesIO(content) as flow:
        workbook = load_workbook(flow, read_only=True, data_only=False, keep_links=False)
        try:
            if not set(EXPECTED_SHEETS) <= set(workbook.sheetnames):
                raise StructureError("The workbook lacks required client sheets.")
            result: dict[str, _ReadSheet] = {}
            for name in EXPECTED_SHEETS:
                sheet = workbook[name]
                if not isinstance(sheet, ReadOnlyWorksheet):
                    raise StructureError("A client sheet is not tabular.")
                sheet.reset_dimensions()
                result[name] = _parse_sheet(sheet, workbook.sheetnames.index(name))
            return result
        finally:
            workbook.close()


def _parse_sheet(sheet: ReadOnlyWorksheet, sheet_id: int) -> _ReadSheet:
    clients: list[ClientRow] = []
    headers: dict[int, str] = {}
    marks: dict[int, set[int]] = {}
    colors_no_supported: set[int] = set()
    valid_name_header = False
    first_column = 4 if sheet.title == "Balanz" else 3
    with closing(sheet.rows) as rows:
        for number_row, row in enumerate(rows, start=1):
            cell_name = _cell(row, 1)
            if number_row == 1:
                value = None if cell_name is None else cell_name.value
                valid_name_header = (
                    isinstance(value, str)
                    and value.strip().casefold() in CLIENT_COLUMN_HEADERS
                    and cell_name is not None
                    and cell_name.data_type not in {"f", "e"}
                )
            elif number_row == WEEK_HEADER_ROW:
                for column in range(first_column, len(row) + 1):
                    cell = _cell(row, column)
                    if cell is not None and cell.value is not None and str(cell.value).strip():
                        if cell.data_type in {"f", "e"}:
                            raise StructureError("Weekly headers cannot be formulas or errors.")
                        headers[column] = str(cell.value)
            elif number_row >= FIRST_CLIENT_ROW:
                value = None if cell_name is None else cell_name.value
                if value is None or (isinstance(value, str) and not value.strip()):
                    continue
                if (
                    not isinstance(value, str)
                    or cell_name is None
                    or cell_name.data_type in {"f", "e"}
                ):
                    raise StructureError(
                        "Client names must be literal text without formulas or errors."
                    )
                clients.append(ClientRow(row=number_row, raw_name=value))
                if headers:
                    for column in range(min(headers), max(headers) + 1):
                        mark = _classify_mark(_cell(row, column))
                        if mark is None:
                            colors_no_supported.add(column)
                        elif mark:
                            marks.setdefault(column, set()).add(number_row)
    if not valid_name_header:
        raise StructureError("Column A must have a Client, Clients, or Name header.")
    first = min(headers) if headers else None
    last = max(headers) if headers else None
    return _ReadSheet(
        info=SheetInfo(
            name=sheet.title,
            sheet_id=sheet_id,
            name_column=1,
            first_week_column=first,
            last_week_column=last,
            last_client_row=clients[-1].row if clients else FIRST_CLIENT_ROW - 1,
        ),
        clients=tuple(clients),
        last_header="" if last is None else headers[last],
        headers=tuple(headers.items()),
        marks={column: frozenset(rows_marked) for column, rows_marked in marks.items()},
        columns_color_no_supported=frozenset(colors_no_supported),
    )


def _cell(row: tuple[object, ...], column: int) -> ReadOnlyCell | None:
    if column > len(row):
        return None
    cell = row[column - 1]
    if isinstance(cell, EmptyCell):
        return None
    if not isinstance(cell, ReadOnlyCell):
        raise StructureError("The reader received an incompatible XLSX cell type.")
    return cell


def _classify_mark(cell: ReadOnlyCell | None) -> bool | None:
    if cell is None:
        return False
    if (
        isinstance(cell.value, str)
        and cell.data_type != "f"
        and cell.value.strip().casefold() == "x"
    ):
        return True
    fill = cell.fill
    if not isinstance(fill, PatternFill) or fill.patternType != "solid":
        return False
    if fill.fgColor.type != "rgb":
        return None
    color = fill.fgColor.rgb
    return isinstance(color, str) and color[-6:].upper() == GREEN_RGB
