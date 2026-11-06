"""Integration coverage for workbook headers and holder separators."""

from pathlib import Path

import pytest
from openpyxl import Workbook

from client_automation.core.normalization import extract_holders
from client_automation.infra.excel_client_reader import ExcelClientReader
from client_automation.infra.excel_client_writer import ExcelClientWriter
from client_automation.infra.system_clock import SystemClock


@pytest.mark.parametrize("header", ["Cliente", "Clientes", "Nombre"])
def test_localized_workbooks_support_reading_and_registration(tmp_path: Path, header: str) -> None:
    workbook_path = tmp_path / "synthetic.xlsx"
    workbook = Workbook()
    first_sheet = workbook.active
    assert first_sheet is not None
    workbook.remove(first_sheet)
    for name in ("Inviu", "IOL", "Bull", "Balanz", "PPI"):
        sheet = workbook.create_sheet(name)
        sheet.cell(1, 1, header)
        sheet.cell(2, 4 if name == "Balanz" else 3, "Example week")
        sheet.cell(3, 1, "Axa Sample")
    workbook.save(workbook_path)
    workbook.close()

    with ExcelClientReader(workbook_path) as reader:
        assert reader.read_clients("IOL")[0].raw_name == "Axa Sample"
        writer = ExcelClientWriter(
            workbook_path, backup_directory=tmp_path / "backups", clock=SystemClock()
        )
        writer.apply_marks(
            "IOL", 3, "Example week", frozenset({3}), expected_revision=reader.revision()
        )
        assert reader.read_marks("IOL", 3) == frozenset({3})


@pytest.mark.parametrize(
    "raw_name",
    ["Axa Y/O Notuka", "Axa Y Notuka", "Axa (esposa de Notuka)"],
)
def test_localized_holder_separators_remain_supported(raw_name: str) -> None:
    assert extract_holders(raw_name) == ("Axa", "Notuka")
