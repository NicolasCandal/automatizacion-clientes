"""Synthetic XLSX writes, backups, and protection against external changes."""

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from openpyxl import Workbook, load_workbook
from openpyxl.cell.rich_text import CellRichText, TextBlock
from openpyxl.cell.text import InlineFont
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.worksheet import Worksheet
from pytest_mock import MockerFixture

from client_automation.core.contracts import ClientWriter
from client_automation.core.errors import (
    ConcurrencyError,
    ConfigurationError,
    PersistenceError,
    StructureError,
)
from client_automation.infra import excel_client_writer
from client_automation.infra.excel_client_reader import ExcelClientReader
from client_automation.infra.excel_client_writer import ExcelClientWriter


class FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 11, 5, 12, 30, tzinfo=UTC)


def get_sheet(workbook: Workbook, name: str = "IOL") -> Worksheet:
    sheet = workbook[name]
    assert isinstance(sheet, Worksheet)
    return sheet


def edit(path: Path, modify: Callable[[Workbook], None]) -> None:
    workbook = load_workbook(path, data_only=False)
    try:
        modify(workbook)
        workbook.save(path)
    finally:
        workbook.close()


def revision(path: Path) -> str:
    with ExcelClientReader(path) as reader:
        return reader.revision()


@pytest.fixture
def path_excel(tmp_path: Path) -> Path:
    workbook = Workbook()
    initial = workbook.active
    assert isinstance(initial, Worksheet)
    workbook.remove(initial)
    for name in ("Inviu", "IOL", "Bull", "Balanz", "PPI"):
        sheet: Worksheet = workbook.create_sheet(name)
        sheet.cell(1, 1, "Client")
        sheet.cell(2, 4 if name == "Balanz" else 3, "Previous week")
        sheet.cell(3, 1, "Axa Sample")
    sheet = get_sheet(workbook)
    sheet.cell(5, 1, "Notuka Example")
    sheet.cell(8, 1, "Axa Sample and/or Notuka Example")
    sheet.cell(3, 2, "=100+200")
    sheet.cell(2, 4, " Current week ")
    sheet.cell(3, 4, "x")
    sheet.cell(5, 4).fill = PatternFill(patternType="solid", fgColor="FF76923C")
    sheet.cell(4, 4, "Note outside of clients")
    sheet.cell(3, 3, "=1+2")
    sheet.cell(2, 4).font = Font(bold=True, color="FF123456")
    sheet.cell(2, 4).alignment = Alignment(horizontal="center")
    sheet.cell(2, 4).number_format = "@"
    sheet.column_dimensions["D"].width = 23
    sheet.cell(8, 4).font = Font(italic=True)
    get_sheet(workbook, "Balanz").cell(3, 3, "=50+50")
    path = tmp_path / "clients.xlsx"
    try:
        workbook.save(path)
    finally:
        workbook.close()
    return path


@pytest.fixture
def writer(path_excel: Path, tmp_path: Path) -> ExcelClientWriter:
    return ExcelClientWriter(
        path_excel,
        backup_directory=tmp_path / "backups",
        clock=FixedClock(),
    )


def test_constructor_no_accesses_on_disk(tmp_path: Path, mocker: MockerFixture) -> None:
    read = mocker.patch.object(Path, "read_bytes")
    writer: ClientWriter = ExcelClientWriter(
        tmp_path / "missing.XLSX",
        backup_directory=tmp_path / "copies",
        clock=FixedClock(),
    )
    assert writer is not None
    read.assert_not_called()
    assert not (tmp_path / "copies").exists()


@pytest.mark.parametrize("extension", [".xls", ".xlsm", ".csv", ""])
def test_rejects_extensions_no_supported(tmp_path: Path, extension: str) -> None:
    with pytest.raises(ConfigurationError):
        ExcelClientWriter(
            tmp_path / ("clients" + extension),
            backup_directory=tmp_path,
            clock=FixedClock(),
        )


def test_backup_exact_and_exclusive_although_the_clock_repeats(
    path_excel: Path,
    writer: ExcelClientWriter,
) -> None:
    original = path_excel.read_bytes()
    first = writer.create_backup()
    second = writer.create_backup()
    assert first != second
    assert first.suffix == ".xlsx"
    assert first.name.startswith("clients_20261105T123000_000000Z_")
    assert first.read_bytes() == second.read_bytes() == original
    assert path_excel.read_bytes() == original
    assert set(first.parent.iterdir()) == {first, second}


def test_rejects_clock_without_zone_before_of_create_files(
    writer: ExcelClientWriter,
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    mocker.patch.object(FixedClock, "now", return_value=datetime(2026, 11, 5))
    with pytest.raises(ConfigurationError):
        writer.create_backup()
    assert not (tmp_path / "backups").exists()


def test_failure_synchronization_backup_cleans_copy_incomplete(
    path_excel: Path,
    writer: ExcelClientWriter,
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    original = path_excel.read_bytes()
    cause = OSError("disk full")
    mocker.patch.object(excel_client_writer, "fsync", side_effect=cause)
    with pytest.raises(PersistenceError) as error:
        writer.create_backup()
    assert error.value.__cause__ is cause
    assert list((tmp_path / "backups").iterdir()) == []
    assert path_excel.read_bytes() == original


def test_backup_detects_change_and_removes_copy_no_confirmed(
    path_excel: Path,
    writer: ExcelClientWriter,
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    original = path_excel.read_bytes()
    mocker.patch.object(Path, "read_bytes", side_effect=[original, b"version externa"])
    with pytest.raises(ConcurrencyError):
        writer.create_backup()
    assert list((tmp_path / "backups").iterdir()) == []


def test_refreshes_only_week_selected_and_preserves_gaps_and_formulas(
    path_excel: Path,
    writer: ExcelClientWriter,
) -> None:
    writer.apply_marks(
        "IOL", 4, "Current week", frozenset({8}), expected_revision=revision(path_excel)
    )
    with ExcelClientReader(path_excel) as reader:
        assert reader.read_marks("IOL", 4) == frozenset({8})
        assert tuple(client.row for client in reader.read_clients("IOL")) == (3, 5, 8)
    workbook = load_workbook(path_excel, data_only=False)
    try:
        sheet = get_sheet(workbook)
        assert sheet.cell(2, 4).value == " Current week "
        assert sheet.cell(3, 2).value == "=100+200"
        assert sheet.cell(3, 3).value == "=1+2"
        assert sheet.cell(4, 4).value == "Note outside of clients"
        assert sheet.cell(3, 4).value is None
        assert sheet.cell(5, 4).fill.fgColor.rgb == "FFFFFFFF"
        assert sheet.cell(8, 4).fill.fgColor.rgb == "FF76923C"
        assert sheet.cell(8, 4).font.italic
        assert get_sheet(workbook, "Balanz").cell(3, 3).value == "=50+50"
    finally:
        workbook.close()


def test_repetition_no_creates_other_week_and_empty_selection_removes_marks(
    path_excel: Path,
    writer: ExcelClientWriter,
) -> None:
    for _ in range(2):
        writer.apply_marks(
            "IOL",
            4,
            "Current week",
            frozenset({3, 8}),
            expected_revision=revision(path_excel),
        )
    with ExcelClientReader(path_excel) as reader:
        assert reader.last_week("IOL") == (4, " Current week ")
        assert reader.read_marks("IOL", 4) == frozenset({3, 8})
    writer.apply_marks(
        "IOL", 4, "Current week", frozenset(), expected_revision=revision(path_excel)
    )
    with ExcelClientReader(path_excel) as reader:
        assert reader.read_marks("IOL", 4) == frozenset()


def test_appends_week_copy_only_format_and_width_of_header_previous(
    path_excel: Path,
    writer: ExcelClientWriter,
) -> None:
    writer.apply_marks(
        "IOL", 5, " New week ", frozenset({5}), expected_revision=revision(path_excel)
    )
    workbook = load_workbook(path_excel, data_only=False)
    try:
        sheet = get_sheet(workbook)
        assert sheet.cell(2, 5).value == "New week"
        assert sheet.cell(2, 5).style_id == sheet.cell(2, 4).style_id
        assert sheet.cell(2, 5).number_format == "@"
        assert sheet.column_dimensions["E"].width == 23
        assert sheet.cell(3, 4).value == "x"
    finally:
        workbook.close()
    with ExcelClientReader(path_excel) as reader:
        assert reader.read_marks("IOL", 5) == frozenset({5})


@pytest.mark.parametrize(("name", "first"), [("PPI", 3), ("Balanz", 4)])
def test_first_week_respects_columns_fixed(
    path_excel: Path,
    writer: ExcelClientWriter,
    name: str,
    first: int,
) -> None:
    def strip_week(workbook: Workbook) -> None:
        get_sheet(workbook, name).cell(2, first).value = None

    edit(path_excel, strip_week)
    writer.apply_marks(name, first, "First", frozenset({3}), expected_revision=revision(path_excel))
    with ExcelClientReader(path_excel) as reader:
        assert reader.last_week(name) == (first, "First")
        assert reader.read_marks(name, first) == frozenset({3})


@pytest.mark.parametrize(
    ("column", "header", "rows"),
    [
        (2, "New week", frozenset({3})),
        (4, "New week", frozenset({3})),
        (6, "New week", frozenset({3})),
        (5, "Current week", frozenset({3})),
        (4, "Current week", frozenset({4})),
        (4, "Current week", frozenset({999})),
        (4, "Current week", frozenset({2})),
        (4, "Current week", frozenset({True})),
        (0, "Current week", frozenset({3})),
        (16385, "Current week", frozenset({3})),
        (True, "Current week", frozenset({3})),
        (4, " ", frozenset({3})),
        (4, "=1+2", frozenset({3})),
        (4, "#REF!", frozenset({3})),
        (4, "Week\u0000", frozenset({3})),
        (4, "a" * 32768, frozenset({3})),
    ],
)
def test_request_invalid_no_changes_file(
    path_excel: Path,
    writer: ExcelClientWriter,
    column: int,
    header: str,
    rows: frozenset[int],
) -> None:
    original = path_excel.read_bytes()
    with pytest.raises(StructureError):
        writer.apply_marks("IOL", column, header, rows, expected_revision=revision(path_excel))
    assert path_excel.read_bytes() == original
    assert not list(path_excel.parent.glob(".excel-*"))


@pytest.mark.parametrize("sheet", ["Other", "iol"])
def test_rejects_sheet_no_configured(writer: ExcelClientWriter, sheet: str) -> None:
    with pytest.raises(StructureError):
        writer.apply_marks(sheet, 4, "Current week", frozenset(), expected_revision="revision")


def test_rejects_revision_empty(writer: ExcelClientWriter) -> None:
    with pytest.raises(StructureError):
        writer.apply_marks("IOL", 4, "Current week", frozenset(), expected_revision=" ")


def test_revision_stale_no_load_nor_saves(
    path_excel: Path,
    writer: ExcelClientWriter,
    mocker: MockerFixture,
) -> None:
    original = path_excel.read_bytes()
    load = mocker.spy(excel_client_writer, "load_workbook")
    with pytest.raises(ConcurrencyError):
        writer.apply_marks("IOL", 4, "Current week", frozenset({3}), expected_revision="stale")
    load.assert_not_called()
    assert path_excel.read_bytes() == original


@pytest.mark.parametrize("value", ["=1+2", "Other record", 10, "#REF!"])
def test_rejects_data_unrelated_or_formulas_in_any_client_of_the_week(
    path_excel: Path,
    writer: ExcelClientWriter,
    value: str | int,
) -> None:
    edit(path_excel, lambda workbook: setattr(get_sheet(workbook).cell(8, 4), "value", value))
    original = path_excel.read_bytes()
    with pytest.raises(StructureError):
        writer.apply_marks(
            "IOL", 4, "Current week", frozenset({3}), expected_revision=revision(path_excel)
        )
    assert path_excel.read_bytes() == original


@pytest.mark.parametrize(
    "case",
    [
        "protected",
        "merged",
        "duplicate",
        "missing_sheet",
        "name",
        "formula_name",
        "formula_week",
    ],
)
def test_rejects_structures_ambiguous_without_save(
    path_excel: Path,
    writer: ExcelClientWriter,
    case: str,
) -> None:
    def modify(workbook: Workbook) -> None:
        sheet = get_sheet(workbook)
        if case == "protected":
            sheet.protection.sheet = True
        elif case == "merged":
            sheet.merge_cells("D8:D9")
        elif case == "duplicate":
            sheet.cell(2, 5, "Current week")
        elif case == "missing_sheet":
            workbook.remove(get_sheet(workbook, "PPI"))
        elif case == "name":
            sheet.cell(1, 1, "Unknown")
        elif case == "formula_name":
            sheet.cell(8, 1, "=A3")
        else:
            sheet.cell(2, 5, "=D2")

    edit(path_excel, modify)
    original = path_excel.read_bytes()
    with pytest.raises(StructureError):
        writer.apply_marks(
            "IOL", 4, "Current week", frozenset({3}), expected_revision=revision(path_excel)
        )
    assert path_excel.read_bytes() == original


def test_detects_change_during_serialization_and_preserves_version_external(
    path_excel: Path,
    writer: ExcelClientWriter,
    mocker: MockerFixture,
) -> None:
    expected = revision(path_excel)
    serialize = excel_client_writer._serialize
    external: list[bytes] = []

    def serialize_and_change(workbook: Workbook) -> bytes:
        content = serialize(workbook)
        edit(path_excel, lambda other: setattr(get_sheet(other).cell(5, 2), "value", 1234))
        external.append(path_excel.read_bytes())
        return content

    mocker.patch.object(excel_client_writer, "_serialize", side_effect=serialize_and_change)
    with pytest.raises(ConcurrencyError):
        writer.apply_marks("IOL", 4, "Current week", frozenset({3}), expected_revision=expected)
    assert path_excel.read_bytes() == external[0]
    assert not list(path_excel.parent.glob(".excel-*"))


@pytest.mark.parametrize("operation", ["serialize", "synchronize", "replace"])
def test_failures_of_saved_preserve_original_and_clean_temporary(
    path_excel: Path,
    writer: ExcelClientWriter,
    mocker: MockerFixture,
    operation: str,
) -> None:
    original = path_excel.read_bytes()
    cause = OSError("failure simulated")
    if operation == "serialize":
        mocker.patch.object(excel_client_writer, "_serialize", side_effect=cause)
    elif operation == "synchronize":
        mocker.patch.object(excel_client_writer, "fsync", side_effect=cause)
    else:
        replacement = mocker.patch.object(Path, "replace", side_effect=cause)
    with pytest.raises(PersistenceError) as error:
        writer.apply_marks(
            "IOL", 4, "Current week", frozenset({3}), expected_revision=revision(path_excel)
        )
    assert error.value.__cause__ is cause
    assert path_excel.read_bytes() == original
    assert not list(path_excel.parent.glob(".excel-*"))
    if operation == "replace":
        assert replacement.call_count == 1


class FakeWindowsLockError(OSError):
    """Simulate a Windows sharing violation on any operating system."""

    def __init__(self) -> None:
        super().__init__("Simulated sharing violation.")
        self.winerror: int = 32


def test_retries_only_lock_transient_and_again_to_check_revision(
    path_excel: Path,
    writer: ExcelClientWriter,
    mocker: MockerFixture,
) -> None:
    replace = Path.replace
    calls = 0

    def replace_with_lock(temporary: Path, destination: Path) -> Path:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise FakeWindowsLockError()
        return replace(temporary, destination)

    mocker.patch.object(Path, "replace", autospec=True, side_effect=replace_with_lock)
    check = mocker.spy(writer, "_check_revision")
    writer.apply_marks(
        "IOL", 4, "Current week", frozenset({8}), expected_revision=revision(path_excel)
    )
    assert calls == check.call_count == 2
    with ExcelClientReader(path_excel) as reader:
        assert reader.read_marks("IOL", 4) == frozenset({8})


def test_lock_persistent_limits_to_four_attempts(
    path_excel: Path,
    writer: ExcelClientWriter,
    mocker: MockerFixture,
) -> None:
    original = path_excel.read_bytes()
    replacement = mocker.patch.object(Path, "replace", side_effect=FakeWindowsLockError())
    with pytest.raises(PersistenceError):
        writer.apply_marks(
            "IOL", 4, "Current week", frozenset({3}), expected_revision=revision(path_excel)
        )
    assert replacement.call_count == 4
    assert path_excel.read_bytes() == original
    assert not list(path_excel.parent.glob(".excel-*"))


def test_change_between_retries_cancels_without_overwrite(
    path_excel: Path,
    writer: ExcelClientWriter,
    mocker: MockerFixture,
) -> None:
    external: list[bytes] = []

    def block_and_change(temporary: Path, destination: Path) -> Path:
        edit(destination, lambda workbook: setattr(get_sheet(workbook).cell(5, 2), "value", 999))
        external.append(destination.read_bytes())
        raise FakeWindowsLockError()

    replacement = mocker.patch.object(Path, "replace", autospec=True, side_effect=block_and_change)
    with pytest.raises(ConcurrencyError):
        writer.apply_marks(
            "IOL", 4, "Current week", frozenset({8}), expected_revision=revision(path_excel)
        )
    assert replacement.call_count == 1
    assert path_excel.read_bytes() == external[0]
    assert not list(path_excel.parent.glob(".excel-*"))


@pytest.mark.parametrize("operation", ["backup", "marks"])
def test_file_missing_is_error_of_persistence(
    path_excel: Path,
    writer: ExcelClientWriter,
    operation: str,
) -> None:
    path_excel.unlink()
    with pytest.raises(PersistenceError) as error:
        if operation == "backup":
            writer.create_backup()
        else:
            writer.apply_marks("IOL", 4, "Current week", frozenset(), expected_revision="expected")
    assert isinstance(error.value.__cause__, FileNotFoundError)


def test_file_corrupt_no_overwrites(path_excel: Path, writer: ExcelClientWriter) -> None:
    path_excel.write_bytes(b"no es un libro")
    with pytest.raises(StructureError):
        writer.apply_marks(
            "IOL", 4, "Current week", frozenset(), expected_revision=revision(path_excel)
        )
    assert path_excel.read_bytes() == b"no es un libro"


def test_rejects_components_that_could_lost_on_save(
    path_excel: Path,
    writer: ExcelClientWriter,
) -> None:
    with ZipFile(path_excel, "a", compression=ZIP_DEFLATED) as package:
        package.writestr("xl/drawings/drawing1.xml", "<drawings/>")
    original = path_excel.read_bytes()
    with pytest.raises(StructureError, match="tabular"):
        writer.apply_marks(
            "IOL", 4, "Current week", frozenset({3}), expected_revision=revision(path_excel)
        )
    assert path_excel.read_bytes() == original


def test_preserves_text_enriched_in_names_and_columns_fixed(
    path_excel: Path,
    writer: ExcelClientWriter,
) -> None:
    def enrich(workbook: Workbook) -> None:
        sheet = get_sheet(workbook)
        sheet.cell(5, 1).value = CellRichText(TextBlock(InlineFont(b=True), "Notuka Example"))
        sheet.cell(5, 2).value = CellRichText(TextBlock(InlineFont(i=True), "Detail"))

    edit(path_excel, enrich)
    writer.apply_marks(
        "IOL", 4, "Current week", frozenset({5}), expected_revision=revision(path_excel)
    )
    workbook = load_workbook(path_excel, rich_text=True)
    try:
        name = get_sheet(workbook).cell(5, 1).value
        detail = get_sheet(workbook).cell(5, 2).value
        assert isinstance(name, CellRichText)
        assert isinstance(detail, CellRichText)
        assert str(name) == "Notuka Example"
        assert str(detail) == "Detail"
    finally:
        workbook.close()


def test_logs_no_include_names_paths_nor_headers(
    path_excel: Path,
    writer: ExcelClientWriter,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("INFO"):
        writer.create_backup()
        writer.apply_marks(
            "IOL", 4, "Current week", frozenset({3}), expected_revision=revision(path_excel)
        )
    assert "Excel backup created" in caplog.text
    assert "1 clients marked" in caplog.text
    assert "Axa Sample" not in caplog.text
    assert "Current week" not in caplog.text
    assert str(path_excel) not in caplog.text
