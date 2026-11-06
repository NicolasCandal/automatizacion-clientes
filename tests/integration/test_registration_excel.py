"""Complete XLSX registration with temporary JSON sources and no real personal data."""

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook
from openpyxl.styles import PatternFill
from openpyxl.worksheet.worksheet import Worksheet
from pytest_mock import MockerFixture

from client_automation.core.errors import ConcurrencyError, RegistrationInterruptedError
from client_automation.core.models import (
    ClientCatalog,
    WeeklyContacts,
    WeeklyResolution,
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
from client_automation.services.registration import RegistrationService


@dataclass
class Scenario:
    path: Path
    reader: ExcelClientReader
    writer: ExcelClientWriter
    service_catalog: CatalogService
    catalog: ClientCatalog
    week: WeeklyContacts
    resolution: WeeklyResolution

    def service(self) -> RegistrationService:
        return RegistrationService(
            reader_clients=self.reader,
            writer_clients=self.writer,
            source_id="excel_synthetic",
        )


@pytest.fixture
def scenario(tmp_path: Path) -> Iterator[Scenario]:
    path = tmp_path / "clients.xlsx"
    workbook = Workbook()
    initial = workbook.active
    assert isinstance(initial, Worksheet)
    workbook.remove(initial)
    for name in ("Inviu", "IOL", "Bull", "Balanz", "PPI"):
        sheet: Worksheet = workbook.create_sheet(name)
        sheet.cell(1, 1, "Client")
        first = 4 if name == "Balanz" else 3
        sheet.cell(2, first, "02 on 06-11")
        sheet.cell(3, 1, "Axa Sample")
        sheet.cell(5, 1, "Notuka Example")
        sheet.cell(3, 2, "=100+200")
        if name == "IOL":
            sheet.cell(8, 1, "AXA SAMPLE AND/OR NOTUKA EXAMPLE")
            sheet.cell(5, first).fill = PatternFill(patternType="solid", fgColor="FF76923C")
    try:
        workbook.save(path)
    finally:
        workbook.close()
    path_contacts = tmp_path / "week.json"
    save_json(
        path_contacts,
        {
            "period": {"start_date": "2026-11-02", "end_date": "2026-11-06"},
            "contacts": [
                {"raw_name": "Axa Sample (IOL/Balanz)", "last_activity_date": "2026-11-05"}
            ],
        },
    )
    catalogs = JsonCatalogStore(tmp_path / "catalog.json")
    contacts = JsonContactReader(path_contacts)
    matching = MatchingService(
        reader_contacts=contacts,
        store_catalog=catalogs,
        store_aliases=JsonAliasStore(tmp_path / "aliases.json"),
        list_exclusion=JsonExclusionList(tmp_path / "exclusions.json"),
        store_resolutions=JsonResolutionStore(tmp_path / "resolution.json"),
        comparator=RapidFuzzComparator(),
    )
    with ExcelClientReader(path) as reader:
        service_catalog = CatalogService(
            reader_clients=reader,
            store_catalog=catalogs,
            clock=SystemClock(),
            source_id="excel_synthetic",
        )
        catalog = service_catalog.run(dry_run=False)
        yield Scenario(
            path=path,
            reader=reader,
            writer=ExcelClientWriter(
                path, backup_directory=tmp_path / "backups", clock=SystemClock()
            ),
            service_catalog=service_catalog,
            catalog=catalog,
            week=contacts.read(),
            resolution=matching.run(),
        )


def test_dry_run_and_registration_replace_only_week_selected_with_backup_exact(
    scenario: Scenario,
    tmp_path: Path,
) -> None:
    original = scenario.path.read_bytes()
    service = scenario.service()
    plan = service.plan(
        catalog=scenario.catalog,
        week=scenario.week,
        resolution=scenario.resolution,
        header="02 on 06-11",
    )
    result_simulated = service.run(plan)
    assert result_simulated.dry_run
    assert scenario.path.read_bytes() == original
    assert not (tmp_path / "backups").exists()
    assert not (tmp_path / "aliases.json").exists()
    assert not (tmp_path / "resolution.json").exists()
    result = service.run(plan, dry_run=False)
    assert result.applied_sheets == ("IOL", "Balanz")
    assert isinstance(result.backup, Path)
    assert result.backup.read_bytes() == original
    assert scenario.reader.read_marks("IOL", 3) == frozenset({3, 8})
    assert scenario.reader.read_marks("Balanz", 4) == frozenset({3})
    assert scenario.reader.read_marks("Inviu", 3) == frozenset()
    assert result.revision_final == scenario.reader.revision()
    workbook = load_workbook(scenario.path, data_only=False)
    try:
        for name in workbook.sheetnames:
            sheet = workbook[name]
            assert isinstance(sheet, Worksheet)
            assert sheet.cell(3, 2).value == "=100+200"
    finally:
        workbook.close()
    with pytest.raises(ConcurrencyError):
        service.run(plan, dry_run=False)
    catalog_current = scenario.service_catalog.run(dry_run=False)
    assert catalog_current.people == scenario.catalog.people
    new_plan = service.plan(
        catalog=catalog_current,
        week=scenario.week,
        resolution=scenario.resolution,
        header="02 on 06-11",
    )
    revision = scenario.reader.revision()
    repeated = service.run(new_plan, dry_run=False)
    assert repeated.applied_sheets == ()
    assert repeated.backup is None
    assert scenario.reader.revision() == revision
    assert len(list((tmp_path / "backups").iterdir())) == 1


def test_week_new_creates_in_all_the_sheets_and_no_touches_the_previous(
    scenario: Scenario,
) -> None:
    previous = {
        sheet.name: scenario.reader.read_marks(sheet.name, sheet.last_week_column or 3)
        for sheet in scenario.catalog.sheets
    }
    service = scenario.service()
    plan = service.plan(
        catalog=scenario.catalog,
        week=scenario.week,
        resolution=scenario.resolution,
        header="2026-11-02 / 2026-11-06",
    )
    result = service.run(plan, dry_run=False)
    assert result.applied_sheets == ("Inviu", "IOL", "Bull", "Balanz", "PPI")
    for sheet in scenario.catalog.sheets:
        assert (
            scenario.reader.read_marks(sheet.name, sheet.last_week_column or 3)
            == previous[sheet.name]
        )
    assert scenario.reader.last_week("Balanz") == (5, "2026-11-02 / 2026-11-06")
    assert scenario.reader.read_marks("IOL", 4) == frozenset({3, 8})
    assert scenario.reader.read_marks("Balanz", 5) == frozenset({3})


def test_change_real_during_one_write_stops_registration_and_preserves_backup(
    scenario: Scenario,
    mocker: MockerFixture,
) -> None:
    original = scenario.path.read_bytes()
    service = scenario.service()
    plan = service.plan(
        catalog=scenario.catalog,
        week=scenario.week,
        resolution=scenario.resolution,
        header="02 on 06-11",
    )
    apply = scenario.writer.apply_marks

    def apply_and_change(
        sheet: str, column: int, header: str, rows: frozenset[int], *, expected_revision: str
    ) -> None:
        apply(sheet, column, header, rows, expected_revision=expected_revision)
        workbook = load_workbook(scenario.path, data_only=False)
        try:
            destination = workbook["Balanz"]
            assert isinstance(destination, Worksheet)
            destination.cell(3, 1, "Client modified externally")
            workbook.save(scenario.path)
        finally:
            workbook.close()

    write = mocker.patch.object(scenario.writer, "apply_marks", side_effect=apply_and_change)
    with pytest.raises(RegistrationInterruptedError) as error:
        service.run(plan, dry_run=False)
    assert isinstance(error.value.__cause__, ConcurrencyError)
    assert isinstance(error.value.backup, Path)
    assert error.value.backup.read_bytes() == original
    assert error.value.confirmed_sheets == ()
    assert error.value.current_sheet == "IOL"
    assert write.call_count == 1
    assert scenario.reader.read_clients("Balanz")[0].raw_name == "Client modified externally"
    assert scenario.reader.read_marks("Balanz", 4) == frozenset()
