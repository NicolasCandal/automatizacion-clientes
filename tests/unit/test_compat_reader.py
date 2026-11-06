from pathlib import Path

from openpyxl import Workbook
from openpyxl.worksheet.worksheet import Worksheet

from catalog_index.reader import build_catalog, count_people_across_sheets
from repository.repo_xlsx import RepoXlsx

SHEETS = ["Inviu", "IOL", "Bull", "Balanz", "PPI"]


def _build_excel_fixture(tmp_path: Path) -> Path:
    wb = Workbook()
    initial = wb.active
    assert isinstance(initial, Worksheet)
    wb.remove(initial)

    clients_common = ["Fedezu, Lidexe Bekaxo", "PEXEZA LEVOBA", "Goxane Fuzifa DELEVE"]
    for sheet_name in ["Inviu", "IOL", "Bull", "PPI"]:
        ws = wb.create_sheet(sheet_name)
        ws.cell(row=1, column=1, value="Client")
        ws.cell(row=1, column=2, value="Capital")
        ws.cell(row=2, column=3, value="15 on 19-12")
        ws.cell(row=2, column=4, value="22 on 26-12")
        for i, name in enumerate(clients_common):
            row = 3 + i
            ws.cell(row=row, column=1, value=name)
            ws.cell(row=row, column=2, value=1000 * (i + 1))

    ws = wb.create_sheet("Balanz")
    ws.cell(row=1, column=1, value="CLIENTS")
    ws.cell(row=1, column=2, value="Capital")
    ws.cell(row=1, column=3, value="Capital 2")
    ws.cell(row=2, column=4, value="15 on 19-12")
    ws.cell(row=3, column=1, value="Lidexe Bekaxo Fedezu")
    ws.cell(row=4, column=1, value="RAXOZI GOGOTE AND/OR RETUBO GEZIGE RATIXU")
    ws.cell(row=5, column=1, value="MOPIVO BEMIBE (wife Gosoru Fezixi)")

    path = tmp_path / "fixture.xlsx"
    wb.save(path)
    return path


def test_build_catalog_structure_by_sheet(tmp_path: Path) -> None:
    repo = RepoXlsx(_build_excel_fixture(tmp_path))
    catalog = build_catalog(repo, "fixture")

    assert set(catalog["sheets"].keys()) == set(SHEETS)
    assert catalog["sheets"]["IOL"]["first_week_column"] == 3
    assert catalog["sheets"]["IOL"]["last_week_column"] == 4
    assert catalog["sheets"]["IOL"]["client_rows"] == 3

    assert catalog["sheets"]["Balanz"]["first_week_column"] == 4
    assert catalog["sheets"]["Balanz"]["last_week_column"] == 4
    assert catalog["sheets"]["Balanz"]["client_rows"] == 3

    assert "revision" in catalog
    assert catalog["spreadsheet_id"] == "fixture"


def test_deduplication_between_sheets(tmp_path: Path) -> None:
    repo = RepoXlsx(_build_excel_fixture(tmp_path))
    catalog = build_catalog(repo, "fixture")

    people_fedezu = [p for p in catalog["people"] if p["sorted_name_key"] == "bekaxo fedezu lidexe"]
    assert len(people_fedezu) == 1
    sheets_fedezu = {a["sheet"] for a in people_fedezu[0]["occurrences"]}
    assert sheets_fedezu == {"Inviu", "IOL", "Bull", "PPI", "Balanz"}

    assert count_people_across_sheets(catalog) >= 1


def test_ownership_shared_generates_people_separate_same_row(tmp_path: Path) -> None:
    repo = RepoXlsx(_build_excel_fixture(tmp_path))
    catalog = build_catalog(repo, "fixture")

    person_gogote = next(p for p in catalog["people"] if p["canonical_name"] == "RAXOZI GOGOTE")
    person_gezige = next(
        p for p in catalog["people"] if p["canonical_name"] == "RETUBO GEZIGE RATIXU"
    )
    assert person_gogote["occurrences"][0]["row"] == person_gezige["occurrences"][0]["row"]
    assert person_gogote["occurrences"][0]["sheet"] == "Balanz"


def test_ownership_parentheses_wife_as_person_extra(tmp_path: Path) -> None:
    repo = RepoXlsx(_build_excel_fixture(tmp_path))
    catalog = build_catalog(repo, "fixture")

    person_gosoru = next(p for p in catalog["people"] if p["canonical_name"] == "Gosoru Fezixi")
    assert person_gosoru["occurrences"][0]["raw_name"] == "MOPIVO BEMIBE (wife Gosoru Fezixi)"


def test_revision_stable_if_file_no_changes(tmp_path: Path) -> None:
    path = _build_excel_fixture(tmp_path)
    repo1 = RepoXlsx(path)
    repo2 = RepoXlsx(path)
    assert (
        build_catalog(repo1, "fixture")["revision"] == build_catalog(repo2, "fixture")["revision"]
    )
