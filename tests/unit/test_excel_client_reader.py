"""Synthetic workbook reads, marks, and snapshot lifecycle."""

import re
from collections.abc import Callable
from io import BytesIO
from os import utime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Color, PatternFill
from openpyxl.worksheet.worksheet import Worksheet
from pytest_mock import MockerFixture

from client_automation.core.contracts import ClientReader
from client_automation.core.errors import (
    ConfigurationError,
    PersistenceError,
    StructureError,
)
from client_automation.infra import excel_client_reader
from client_automation.infra.excel_client_reader import ExcelClientReader


def get_sheet(workbook: Workbook, name: str) -> Worksheet:
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


@pytest.fixture
def path_excel(tmp_path: Path) -> Path:
    workbook = Workbook()
    initial = workbook.active
    assert isinstance(initial, Worksheet)
    workbook.remove(initial)
    for name in ("Inviu", "IOL", "Bull", "Balanz", "PPI"):
        sheet: Worksheet = workbook.create_sheet(name)
        sheet.cell(1, 1, "CLIENTS" if name == "Balanz" else "Client")
        sheet.cell(1, 2, "Capital")
        if name != "PPI":
            sheet.cell(2, 4 if name == "Balanz" else 3, "02 on 06-11")
            sheet.cell(3, 1, "Axa Sample")
    balanz = get_sheet(workbook, "Balanz")
    balanz.cell(2, 2, "Capital")
    balanz.cell(2, 3, "Capital 2")
    sheet = get_sheet(workbook, "IOL")
    sheet.cell(2, 4, " 09 on 13-11 ")
    sheet.cell(3, 1, "  Axa Sample  ")
    sheet.cell(3, 2, "=100+200")
    sheet.cell(5, 1, "Notuka Example")
    sheet.cell(6, 1, "   ")
    sheet.cell(8, 1, "AXA SAMPLE AND/OR NOTUKA EXAMPLE")
    sheet.cell(3, 4).fill = PatternFill(patternType="solid", fgColor="FF76923C")
    sheet.cell(5, 4).fill = PatternFill(patternType="solid", fgColor="FFFFFFFF")
    sheet.cell(8, 4, " X ")
    sheet.cell(1000, 4).fill = PatternFill(patternType="solid", fgColor="FF76923C")
    path = tmp_path / "clients.xlsx"
    try:
        workbook.save(path)
    finally:
        workbook.close()
    return path


def test_constructor_no_opens_files(tmp_path: Path, mocker: MockerFixture) -> None:
    read = mocker.patch.object(Path, "read_bytes", side_effect=PermissionError("locked"))
    reader = ExcelClientReader(tmp_path / "missing.xlsx")
    read.assert_not_called()
    reader.close()


def test_detects_structure_real_and_columns_fixed_balanz(path_excel: Path) -> None:
    reader: ClientReader = ExcelClientReader(path_excel)
    sheets = reader.list_sheets()
    by_name = {sheet.name: sheet for sheet in sheets}
    assert tuple(by_name) == ("Inviu", "IOL", "Bull", "Balanz", "PPI")
    assert by_name["IOL"].sheet_id == 1
    assert by_name["IOL"].first_week_column == 3
    assert by_name["IOL"].last_week_column == 4
    assert by_name["IOL"].last_client_row == 8
    assert by_name["Balanz"].first_week_column == 4
    assert by_name["PPI"].first_week_column is None
    assert by_name["PPI"].last_client_row == 2


def test_preserves_clients_after_of_gaps_and_names_without_trim(path_excel: Path) -> None:
    with ExcelClientReader(path_excel) as reader:
        clients = reader.read_clients("IOL")
    assert tuple(client.row for client in clients) == (3, 5, 8)
    assert clients[0].raw_name == "  Axa Sample  "
    assert clients[2].raw_name == "AXA SAMPLE AND/OR NOTUKA EXAMPLE"


def test_marks_include_green_and_x_and_exclude_rows_without_client(path_excel: Path) -> None:
    with ExcelClientReader(path_excel) as reader:
        assert reader.read_marks("IOL", 4) == frozenset({3, 8})
        assert reader.read_marks("IOL", 3) == frozenset()


def test_last_week_preserves_text_and_absence_of_weeks(path_excel: Path) -> None:
    with ExcelClientReader(path_excel) as reader:
        assert reader.last_week("IOL") == (4, " 09 on 13-11 ")
        assert reader.last_week("PPI") == (1, "")
        assert reader.read_clients("PPI") == ()


def test_headers_complete_preserve_order_columns_and_texts(path_excel: Path) -> None:
    with ExcelClientReader(path_excel) as reader:
        assert reader.read_headers("IOL") == ((3, "02 on 06-11"), (4, " 09 on 13-11 "))
        assert reader.read_headers("Balanz") == ((4, "02 on 06-11"),)
        assert reader.read_headers("PPI") == ()
    with pytest.raises(ConfigurationError):
        reader.read_headers("IOL")


def test_read_no_modifies_bytes_nor_formulas(path_excel: Path) -> None:
    original = path_excel.read_bytes()
    with ExcelClientReader(path_excel) as reader:
        reader.list_sheets()
        reader.read_clients("IOL")
        reader.read_marks("IOL", 4)
        reader.revision()
    assert path_excel.read_bytes() == original
    workbook = load_workbook(path_excel, data_only=False)
    try:
        assert get_sheet(workbook, "IOL").cell(3, 2).value == "=100+200"
    finally:
        workbook.close()


def test_parses_one_time_and_refreshes_by_content_although_no_changes_mtime(
    path_excel: Path, mocker: MockerFixture
) -> None:
    load = mocker.spy(excel_client_reader, "load_workbook")
    with ExcelClientReader(path_excel) as reader:
        reader.list_sheets()
        reader.read_clients("IOL")
        reader.read_marks("IOL", 4)
        reader.last_week("IOL")
        assert load.call_count == 1
        revision = reader.revision()
        stamp = path_excel.stat()

        def modify(workbook: Workbook) -> None:
            get_sheet(workbook, "IOL").cell(5, 1, "Name new")

        edit(path_excel, modify)
        utime(path_excel, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        assert reader.revision() != revision
        assert reader.read_clients("IOL")[1].raw_name == "Name new"
        assert load.call_count == 2


def test_revision_detects_changes_only_of_format(path_excel: Path) -> None:
    with ExcelClientReader(path_excel) as reader:
        revision = reader.revision()

        def modify(workbook: Workbook) -> None:
            get_sheet(workbook, "IOL").cell(5, 4).fill = PatternFill(
                patternType="solid", fgColor="FF76923C"
            )

        edit(path_excel, modify)
        assert reader.revision() != revision
        assert reader.read_marks("IOL", 4) == frozenset({3, 5, 8})


@pytest.mark.parametrize("color", ["0076923C", "FF76923c"])
def test_green_ignores_alpha_and_uppercase(path_excel: Path, color: str) -> None:
    def modify(workbook: Workbook) -> None:
        get_sheet(workbook, "IOL").cell(3, 4).fill = PatternFill(patternType="solid", fgColor=color)

    edit(path_excel, modify)
    assert ExcelClientReader(path_excel).read_marks("IOL", 4) == frozenset({3, 8})


def test_pattern_no_solid_no_confuses_with_fill_green(path_excel: Path) -> None:
    def modify(workbook: Workbook) -> None:
        get_sheet(workbook, "IOL").cell(3, 4).fill = PatternFill(
            patternType="darkGrid", fgColor="FF76923C"
        )

    edit(path_excel, modify)
    assert ExcelClientReader(path_excel).read_marks("IOL", 4) == frozenset({8})


@pytest.mark.parametrize("color", [Color(theme=5), Color(indexed=10)])
def test_colors_no_rgb_no_block_catalog_but_no_ignore_on_read_marks(
    path_excel: Path, color: Color
) -> None:
    def modify(workbook: Workbook) -> None:
        get_sheet(workbook, "IOL").cell(5, 4).fill = PatternFill(patternType="solid", fgColor=color)

    edit(path_excel, modify)
    with ExcelClientReader(path_excel) as reader:
        assert len(reader.read_clients("IOL")) == 3
        with pytest.raises(StructureError, match="RGB"):
            reader.read_marks("IOL", 4)


def test_x_legacy_is_validates_even_with_color_of_theme(path_excel: Path) -> None:
    def modify(workbook: Workbook) -> None:
        get_sheet(workbook, "IOL").cell(8, 4).fill = PatternFill(
            patternType="solid", fgColor=Color(theme=5)
        )

    edit(path_excel, modify)
    assert ExcelClientReader(path_excel).read_marks("IOL", 4) == frozenset({3, 8})


@pytest.mark.parametrize("column", [True, 0, -1, 16385, 2, 5])
def test_column_of_marks_must_be_one_week_existing(path_excel: Path, column: int) -> None:
    with ExcelClientReader(path_excel) as reader, pytest.raises(StructureError):
        reader.read_marks("IOL", column)


def test_no_accepts_read_marks_of_sheet_without_weeks(path_excel: Path) -> None:
    with ExcelClientReader(path_excel) as reader, pytest.raises(StructureError):
        reader.read_marks("PPI", 3)


@pytest.mark.parametrize("value", [123, True, "=B5", "#VALUE!"])
def test_names_no_can_be_numbers_formulas_nor_errors(path_excel: Path, value: int | str) -> None:
    def modify(workbook: Workbook) -> None:
        get_sheet(workbook, "IOL").cell(5, 1, value)

    edit(path_excel, modify)
    with pytest.raises(StructureError, match="literal text"):
        ExcelClientReader(path_excel).read_clients("IOL")


def test_header_of_name_must_be_present(path_excel: Path) -> None:
    def modify(workbook: Workbook) -> None:
        get_sheet(workbook, "IOL").cell(1, 1).value = None

    edit(path_excel, modify)
    with pytest.raises(StructureError, match="header"):
        ExcelClientReader(path_excel).list_sheets()


def test_no_interprets_formulas_as_headers_of_week(path_excel: Path) -> None:
    def modify(workbook: Workbook) -> None:
        get_sheet(workbook, "IOL").cell(2, 4, "=C2")

    edit(path_excel, modify)
    with pytest.raises(StructureError, match="headers"):
        ExcelClientReader(path_excel).last_week("IOL")


def test_sheets_required_missing_and_queries_unknown_reject(path_excel: Path) -> None:
    with ExcelClientReader(path_excel) as reader, pytest.raises(StructureError):
        reader.read_clients("Other sheet")

    def modify(workbook: Workbook) -> None:
        workbook.remove(get_sheet(workbook, "PPI"))

    edit(path_excel, modify)
    with pytest.raises(StructureError, match="required"):
        ExcelClientReader(path_excel).list_sheets()


def test_dimensions_incorrect_no_hide_rows_real(path_excel: Path) -> None:
    with BytesIO(path_excel.read_bytes()) as source, BytesIO() as destination:
        with ZipFile(source) as input, ZipFile(destination, "w", ZIP_DEFLATED) as output:
            for name in input.namelist():
                content = input.read(name)
                if name == "xl/worksheets/sheet2.xml":
                    content = re.sub(b'<dimension ref="[^"]+"', b'<dimension ref="A1:A1"', content)
                output.writestr(name, content)
        path_excel.write_bytes(destination.getvalue())

    assert tuple(client.row for client in ExcelClientReader(path_excel).read_clients("IOL")) == (
        3,
        5,
        8,
    )


def test_file_corrupt_no_reuses_snapshot_previous(path_excel: Path) -> None:
    reader = ExcelClientReader(path_excel)
    assert reader.read_clients("IOL")
    path_excel.write_bytes(b"not a ZIP file")
    with pytest.raises(StructureError) as captured:
        reader.read_clients("IOL")
    assert captured.value.__cause__ is not None


def test_file_missing_no_creates_files(tmp_path: Path) -> None:
    path = tmp_path / "new" / "clients.xlsx"
    with pytest.raises(PersistenceError) as captured:
        ExcelClientReader(path).list_sheets()
    assert isinstance(captured.value.__cause__, FileNotFoundError)
    assert not path.parent.exists()


def test_access_denied_translates_without_expose_paths(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    cause = PermissionError("Path private")
    mocker.patch.object(Path, "read_bytes", side_effect=cause)
    with pytest.raises(PersistenceError) as captured:
        ExcelClientReader(tmp_path / "clients.xlsx").revision()
    assert captured.value.__cause__ is cause
    assert "private" not in str(captured.value)


@pytest.mark.parametrize("extension", ["xls", "xlsm", "csv"])
def test_extensions_no_supported_reject_without_open(tmp_path: Path, extension: str) -> None:
    with pytest.raises(ConfigurationError):
        ExcelClientReader(tmp_path / f"clients.{extension}")


def test_closure_is_idempotent_and_blocks_all_the_reads(path_excel: Path) -> None:
    reader = ExcelClientReader(path_excel)
    reader.list_sheets()
    reader.close()
    reader.close()
    operations: tuple[Callable[[], object], ...] = (
        reader.list_sheets,
        reader.revision,
        reader.__enter__,
        lambda: reader.read_clients("IOL"),
        lambda: reader.last_week("IOL"),
        lambda: reader.read_marks("IOL", 4),
    )
    for operation in operations:
        with pytest.raises(ConfigurationError, match="closed"):
            operation()


def test_context_closes_without_hide_exception(path_excel: Path) -> None:
    reader = ExcelClientReader(path_excel)
    with pytest.raises(RuntimeError, match="sample"), reader:
        reader.list_sheets()
        raise RuntimeError("sample")
    with pytest.raises(ConfigurationError):
        reader.revision()
