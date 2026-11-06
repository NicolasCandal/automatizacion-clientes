"""End-to-end XLSX integration with catalog, report, and alias JSON stores."""

from datetime import date
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook
from openpyxl.worksheet.worksheet import Worksheet
from pytest_mock import MockerFixture

from client_automation.core.errors import ConcurrencyError
from client_automation.core.matching import MatchingEngine
from client_automation.core.models import (
    ClientLocation,
    ClientRow,
    ResolvedContact,
    WeeklyContact,
)
from client_automation.infra.excel_client_reader import ExcelClientReader
from client_automation.infra.excel_client_writer import ExcelClientWriter
from client_automation.infra.json_alias_store import JsonAliasStore
from client_automation.infra.json_catalog_store import JsonCatalogStore
from client_automation.infra.json_contact_reader import JsonContactReader
from client_automation.infra.json_exclusion_list import JsonExclusionList
from client_automation.infra.json_resolution_store import JsonResolutionStore
from client_automation.infra.json_storage import save_json
from client_automation.infra.rapidfuzz_comparator import RapidFuzzComparator
from client_automation.infra.system_clock import SystemClock
from client_automation.services.catalog import CatalogService
from client_automation.services.matching import MatchingService


@pytest.fixture
def path_excel(tmp_path: Path) -> Path:
    workbook = Workbook()
    initial = workbook.active
    assert isinstance(initial, Worksheet)
    workbook.remove(initial)
    for name in ("Inviu", "IOL", "Bull", "Balanz", "PPI"):
        sheet: Worksheet = workbook.create_sheet(name)
        sheet.cell(1, 1, "Client")
        sheet.cell(2, 4 if name == "Balanz" else 3, "02 on 06-11")
        sheet.cell(3, 1, "Axa Sample")
        if name == "IOL":
            sheet.cell(5, 1, "Sample, Axa")
            sheet.cell(8, 1, "Notuka Example")
            sheet.cell(3, 2, "=100+200")
    path = tmp_path / "clients.xlsx"
    try:
        workbook.save(path)
    finally:
        workbook.close()
    return path


def test_excel_feeds_catalog_and_matches_without_modify_source(
    tmp_path: Path, path_excel: Path
) -> None:
    original = path_excel.read_bytes()
    catalogs = JsonCatalogStore(tmp_path / "catalog.json")
    with ExcelClientReader(path_excel) as reader:
        service_catalog = CatalogService(
            reader_clients=reader,
            store_catalog=catalogs,
            clock=SystemClock(),
            source_id="excel_synthetic",
        )
        simulated = service_catalog.run()
        assert not (tmp_path / "catalog.json").exists()
        catalog = service_catalog.run(dry_run=False)
    assert catalog.people == simulated.people
    assert len(catalog.people) == 2
    path_contacts = tmp_path / "week.json"
    save_json(
        path_contacts,
        {
            "period": {"start_date": "2026-11-02", "end_date": "2026-11-06"},
            "contacts": [{"raw_name": "Axa Sample (IOL/Bull)", "last_activity_date": "2026-11-05"}],
        },
    )
    aliases = JsonAliasStore(tmp_path / "aliases.json")
    reports = JsonResolutionStore(tmp_path / "report.json")
    service = MatchingService(
        reader_contacts=JsonContactReader(path_contacts),
        store_catalog=catalogs,
        store_aliases=aliases,
        list_exclusion=JsonExclusionList(tmp_path / "exclusions.json"),
        store_resolutions=reports,
        comparator=RapidFuzzComparator(),
    )

    resolution = service.run(dry_run=False)

    assert len(resolution.resolved) == 1
    assert resolution.resolved[0].locations == (
        ClientLocation(sheet="IOL", row=3),
        ClientLocation(sheet="IOL", row=5),
        ClientLocation(sheet="Bull", row=3),
    )
    assert reports.read() == resolution
    assert (
        JsonAliasStore(tmp_path / "aliases.json").find_by_name("Axa Sample (IOL/Bull)") is not None
    )
    assert path_excel.read_bytes() == original


def test_change_real_of_excel_during_read_no_replaces_catalog(
    tmp_path: Path, path_excel: Path, mocker: MockerFixture
) -> None:
    path_catalog = tmp_path / "catalog.json"
    with ExcelClientReader(path_excel) as reader:
        service = CatalogService(
            reader_clients=reader,
            store_catalog=JsonCatalogStore(path_catalog),
            clock=SystemClock(),
            source_id="excel_synthetic",
        )
        service.run(dry_run=False)
        cache_previous = path_catalog.read_bytes()
        read_original = reader.read_clients

        def read_with_change(name: str) -> tuple[ClientRow, ...]:
            clients = read_original(name)
            if name == "IOL":
                workbook = load_workbook(path_excel, data_only=False)
                try:
                    sheet = workbook["IOL"]
                    assert isinstance(sheet, Worksheet)
                    sheet.cell(8, 1, "Client included during the read")
                    workbook.save(path_excel)
                finally:
                    workbook.close()
            return clients

        mocker.patch.object(reader, "read_clients", side_effect=read_with_change)

        with pytest.raises(ConcurrencyError):
            service.run(dry_run=False, force=True)

    assert path_catalog.read_bytes() == cache_previous


def test_backup_and_marks_of_all_the_occurrences_refresh_catalog(
    tmp_path: Path,
    path_excel: Path,
) -> None:
    original = path_excel.read_bytes()
    writer = ExcelClientWriter(
        path_excel,
        backup_directory=tmp_path / "backups",
        clock=SystemClock(),
    )
    with ExcelClientReader(path_excel) as reader:
        service = CatalogService(
            reader_clients=reader,
            store_catalog=JsonCatalogStore(tmp_path / "catalog.json"),
            clock=SystemClock(),
            source_id="excel_synthetic",
        )
        catalog = service.run(dry_run=False)
        engine = MatchingEngine(catalog, RapidFuzzComparator())
        results = engine.resolve_contact(
            WeeklyContact(
                raw_name="Axa Sample (IOL/Bull)",
                last_activity_date=date(2026, 11, 5),
            )
        )
        assert len(results) == 1
        resolved = results[0]
        assert isinstance(resolved, ResolvedContact)
        backup = writer.create_backup()
        rows_iol = frozenset(
            location.row for location in resolved.locations if location.sheet == "IOL"
        )
        writer.apply_marks("IOL", 3, "02 on 06-11", rows_iol, expected_revision=catalog.revision)
        assert reader.read_marks("IOL", 3) == frozenset({3, 5})
        with pytest.raises(ConcurrencyError):
            writer.apply_marks(
                "Bull", 3, "02 on 06-11", frozenset({3}), expected_revision=catalog.revision
            )
        writer.apply_marks(
            "Bull", 3, "02 on 06-11", frozenset({3}), expected_revision=reader.revision()
        )
        assert reader.read_marks("Bull", 3) == frozenset({3})
        new_catalog = service.run(dry_run=False)
        assert new_catalog.revision != catalog.revision
        assert new_catalog.people == catalog.people
    assert backup.read_bytes() == original
    workbook = load_workbook(path_excel, data_only=False)
    try:
        sheet = workbook["IOL"]
        assert isinstance(sheet, Worksheet)
        assert sheet.cell(3, 2).value == "=100+200"
    finally:
        workbook.close()
