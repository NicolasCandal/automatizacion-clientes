from pathlib import Path

import pytest
from openpyxl import Workbook
from openpyxl.worksheet.worksheet import Worksheet

from repository.repo_xlsx import RepoXlsx
from repository.types import StructureError

SHEETS = ["Inviu", "IOL", "Bull", "Balanz", "PPI"]


def _build_excel_fixture(tmp_path: Path) -> Path:
    wb = Workbook()
    initial = wb.active
    assert isinstance(initial, Worksheet)
    wb.remove(initial)

    for sheet_name in ["Inviu", "IOL", "Bull", "PPI"]:
        ws = wb.create_sheet(sheet_name)
        ws.cell(row=1, column=1, value="Client")
        ws.cell(row=1, column=2, value="Capital")
        ws.cell(row=2, column=3, value="15 on 19-12")
        for i, name in enumerate(["Sivivu, Mizote", "Kukeve, Radono", "Gibelo, Fexira"]):
            row = 3 + i
            ws.cell(row=row, column=1, value=name)
            ws.cell(row=row, column=2, value=1000 * (i + 1))

    ws = wb.create_sheet("Balanz")
    ws.cell(row=1, column=1, value="CLIENTS")
    ws.cell(row=2, column=4, value="15 on 19-12")
    ws.cell(row=3, column=1, value="Voledi, Axa")

    path = tmp_path / "fixture.xlsx"
    wb.save(path)
    return path


def test_list_sheets(tmp_path: Path) -> None:
    repo = RepoXlsx(_build_excel_fixture(tmp_path))
    sheets = {h.name: h for h in repo.list_sheets()}
    assert set(sheets) == set(SHEETS)
    assert sheets["IOL"].first_week_column == 3
    assert sheets["IOL"].last_week_column == 3
    assert sheets["IOL"].last_client_row == 5
    assert sheets["Balanz"].first_week_column == 4
    assert sheets["Balanz"].last_client_row == 3


def test_read_clients(tmp_path: Path) -> None:
    repo = RepoXlsx(_build_excel_fixture(tmp_path))
    clients = repo.read_clients("IOL")
    assert [c.raw_name for c in clients] == [
        "Sivivu, Mizote",
        "Kukeve, Radono",
        "Gibelo, Fexira",
    ]


def test_missing_sheet_raises_error(tmp_path: Path) -> None:
    wb = Workbook()
    initial = wb.active
    assert isinstance(initial, Worksheet)
    wb.remove(initial)
    for sheet_name in ["Inviu", "IOL", "Bull", "Balanz"]:  # PPI is intentionally missing.
        ws = wb.create_sheet(sheet_name)
        ws.cell(row=1, column=1, value="Client")
    path = tmp_path / "incomplete.xlsx"
    wb.save(path)

    with pytest.raises(StructureError):
        RepoXlsx(path).list_sheets()


def test_apply_marks_paints_green_and_white(tmp_path: Path) -> None:
    path = _build_excel_fixture(tmp_path)
    repo = RepoXlsx(path)

    repo.apply_marks(
        "IOL", col=4, header="22 on 26-12", rows={3, 5}, expected_revision=repo.revision()
    )

    marked = repo.read_marks("IOL", col=4)
    assert marked == {3, 5}

    col, header = repo.last_week("IOL")
    assert col == 4
    assert header == "22 on 26-12"


def test_apply_marks_is_idempotent(tmp_path: Path) -> None:
    path = _build_excel_fixture(tmp_path)
    repo = RepoXlsx(path)

    repo.apply_marks(
        "IOL", col=4, header="22 on 26-12", rows={3}, expected_revision=repo.revision()
    )
    repo.apply_marks(
        "IOL", col=4, header="22 on 26-12", rows={3, 4}, expected_revision=repo.revision()
    )
    repo.apply_marks(
        "IOL", col=4, header="22 on 26-12", rows={3, 4}, expected_revision=repo.revision()
    )

    sheets = {h.name: h for h in repo.list_sheets()}
    # Repeated runs must create only one new column, not one per invocation.
    assert sheets["IOL"].last_week_column == 4
    assert repo.read_marks("IOL", col=4) == {3, 4}


def test_create_backup_no_modifies_original(tmp_path: Path) -> None:
    path = _build_excel_fixture(tmp_path)
    repo = RepoXlsx(path)
    original = path.read_bytes()

    path_backup = repo.create_backup()

    assert path.read_bytes() == original
    from pathlib import Path

    assert Path(path_backup).exists()
    assert Path(path_backup).read_bytes() == original


def test_revision_changes_on_write(tmp_path: Path) -> None:
    path = _build_excel_fixture(tmp_path)
    repo = RepoXlsx(path)
    before = repo.revision()

    repo.apply_marks(
        "IOL", col=4, header="22 on 26-12", rows={3}, expected_revision=repo.revision()
    )

    assert repo.revision() != before
