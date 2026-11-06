"""Complete CLI workflow with synthetic inputs and temporary directories."""

import subprocess
import sys
from dataclasses import replace
from os import environ
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook
from openpyxl.worksheet.worksheet import Worksheet
from pytest_mock import MockerFixture

from client_automation import main as input
from client_automation.config import Config
from client_automation.core.errors import PersistenceError
from client_automation.core.models import (
    ClientCatalog,
    WeeklyContacts,
    WeeklyResolution,
)
from client_automation.infra.excel_client_reader import ExcelClientReader
from client_automation.infra.json_alias_store import JsonAliasStore
from client_automation.infra.json_catalog_store import JsonCatalogStore
from client_automation.infra.json_contact_reader import JsonContactReader
from client_automation.infra.json_resolution_store import JsonResolutionStore
from client_automation.infra.json_storage import save_json
from client_automation.services.matching import MatchingService


@pytest.fixture(autouse=True)
def isolate_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(environ):
        if name.upper().startswith("CA_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def configuration(tmp_path: Path) -> Config:
    configuration = Config(base_directory=tmp_path, _env_file=None)
    configuration.excel_path.parent.mkdir(parents=True)
    workbook = Workbook()
    initial = workbook.active
    assert isinstance(initial, Worksheet)
    workbook.remove(initial)
    for name in ("Inviu", "IOL", "Bull", "Balanz", "PPI"):
        sheet: Worksheet = workbook.create_sheet(name)
        sheet.cell(1, 1, "Client")
        sheet.cell(2, 4 if name == "Balanz" else 3, "Previous")
        sheet.cell(3, 1, "Axa Sample")
        sheet.cell(5, 1, "Notuka Example")
        sheet.cell(3, 2, "=100+200")
        if name == "IOL":
            sheet.cell(8, 1, "AXA SAMPLE AND/OR NOTUKA EXAMPLE")
    try:
        workbook.save(configuration.excel_path)
    finally:
        workbook.close()
    write_contacts(configuration, "Axa Sample (IOL/Balanz)")
    return configuration


def write_contacts(configuration: Config, name: str) -> None:
    save_json(
        configuration.contacts_path,
        {
            "period": {"start_date": "2026-11-02", "end_date": "2026-11-06"},
            "contacts": [{"raw_name": name, "last_activity_date": "2026-11-05"}],
        },
    )


def files(base: Path) -> dict[Path, bytes]:
    return {path.relative_to(base): path.read_bytes() for path in base.rglob("*") if path.is_file()}


def arguments(configuration: Config) -> list[str]:
    return [
        "--no-env",
        "--base-directory",
        str(configuration.base_directory),
        "--week-header",
        "2026-11-02 / 2026-11-06",
    ]


def test_dry_run_default_no_creates_nor_modifies_files(
    configuration: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    original = files(configuration.base_directory)
    assert input.main(arguments(configuration)) == 0
    assert files(configuration.base_directory) == original
    output = capsys.readouterr()
    assert output.out == ""
    assert "Sheet plan: IOL; column=4" in output.err
    assert "mark=2" in output.err
    assert "dry_run=True" in output.err
    assert "Axa Sample" not in output.err
    assert str(configuration.excel_path) not in output.err


def test_apply_saves_report_aliases_backup_and_marks_and_accepts_repetition(
    configuration: Config,
) -> None:
    original = configuration.excel_path.read_bytes()
    assert input.main([*arguments(configuration), "--apply"]) == 0
    backups = list(configuration.backup_directory.iterdir())
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    assert JsonCatalogStore(configuration.catalog_path).read() is not None
    assert len(JsonResolutionStore(configuration.resolution_path).read().resolved) == 1
    assert (
        JsonAliasStore(configuration.aliases_path).find_by_name("Axa Sample (IOL/Balanz)")
        is not None
    )
    with ExcelClientReader(configuration.excel_path) as reader:
        assert reader.read_marks("IOL", 4) == frozenset({3, 8})
        assert reader.read_marks("Balanz", 5) == frozenset({3})
        assert reader.read_marks("Bull", 4) == frozenset()
    workbook = load_workbook(configuration.excel_path, data_only=False)
    try:
        sheet = workbook["IOL"]
        assert isinstance(sheet, Worksheet)
        assert sheet.cell(3, 2).value == "=100+200"
    finally:
        workbook.close()
    current = configuration.excel_path.read_bytes()
    assert input.main([*arguments(configuration), "--apply"]) == 0
    assert configuration.excel_path.read_bytes() == current
    assert len(list(configuration.backup_directory.iterdir())) == 1


@pytest.mark.parametrize("apply", [False, True])
def test_pending_return_three_without_marks_and_only_save_report_in_real(
    configuration: Config, apply: bool
) -> None:
    write_contacts(configuration, "Unknown identity zzz 987")
    original = configuration.excel_path.read_bytes()
    options = ["--apply"] if apply else []
    assert input.main([*arguments(configuration), *options]) == 3
    assert configuration.excel_path.read_bytes() == original
    assert not configuration.backup_directory.exists()
    assert not configuration.aliases_path.exists()
    assert configuration.catalog_path.exists() is apply
    assert configuration.resolution_path.exists() is apply


def test_pending_can_include_of_way_explicit(configuration: Config) -> None:
    write_contacts(configuration, "Unknown identity zzz 987")
    assert input.main([*arguments(configuration), "--apply", "--allow-pending"]) == 0
    with ExcelClientReader(configuration.excel_path) as reader:
        assert reader.read_marks("IOL", 4) == frozenset()
    assert not configuration.aliases_path.exists()


def test_dry_run_overrides_over_the_configuration_real(
    configuration: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CA_DRY_RUN", "false")
    original = files(configuration.base_directory)
    assert input.main([*arguments(configuration), "--dry-run"]) == 0
    assert files(configuration.base_directory) == original


def test_mode_real_start_date_environment_respects(
    configuration: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CA_DRY_RUN", "false")
    assert input.main(arguments(configuration)) == 0
    assert configuration.resolution_path.exists()
    assert configuration.backup_directory.exists()


def test_cache_invalid_only_rebuilds_with_option_explicit(configuration: Config) -> None:
    configuration.catalog_path.parent.mkdir(parents=True)
    configuration.catalog_path.write_bytes(b"{invalido")
    original = files(configuration.base_directory)
    assert input.main(arguments(configuration)) == 2
    assert files(configuration.base_directory) == original
    assert input.main([*arguments(configuration), "--rebuild-catalog"]) == 0
    assert files(configuration.base_directory) == original
    assert input.main([*arguments(configuration), "--rebuild-catalog", "--apply"]) == 0
    assert JsonCatalogStore(configuration.catalog_path).read() is not None


def test_error_of_configuration_no_exposes_values(
    configuration: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CA_LOG_LEVEL", "secret")
    assert input.main(arguments(configuration)) == 2
    assert "secret" not in capsys.readouterr().err


def test_closes_reader_even_if_fails_contacts(configuration: Config, mocker: MockerFixture) -> None:
    configuration.contacts_path.write_bytes(b"{invalido")
    closure = mocker.spy(ExcelClientReader, "close")
    assert input.main(arguments(configuration)) == 2
    closure.assert_called_once()


def test_error_of_report_prevents_write_excel(configuration: Config, mocker: MockerFixture) -> None:
    original = configuration.excel_path.read_bytes()
    mocker.patch.object(
        JsonResolutionStore, "save", side_effect=PersistenceError("Simulated failure.")
    )
    assert input.main([*arguments(configuration), "--apply"]) == 4
    assert configuration.excel_path.read_bytes() == original
    assert not configuration.backup_directory.exists()


def test_disagreement_between_preview_and_resolution_prevents_marks(
    configuration: Config, mocker: MockerFixture
) -> None:
    original = configuration.excel_path.read_bytes()
    resolve = MatchingService.run

    def resolve_with_change(
        self: MatchingService,
        *,
        dry_run: bool = True,
        catalog: ClientCatalog | None = None,
        week: WeeklyContacts | None = None,
    ) -> WeeklyResolution:
        result = resolve(self, dry_run=dry_run, catalog=catalog, week=week)
        return result if dry_run else replace(result, warnings=("Change simulated.",))

    mocker.patch.object(MatchingService, "run", autospec=True, side_effect=resolve_with_change)
    assert input.main([*arguments(configuration), "--apply"]) == 5
    assert configuration.excel_path.read_bytes() == original
    assert not configuration.backup_directory.exists()


def test_contacts_read_one_single_time_in_execution_real(
    configuration: Config, mocker: MockerFixture
) -> None:
    read = mocker.spy(JsonContactReader, "read")
    assert input.main([*arguments(configuration), "--apply"]) == 0
    read.assert_called_once()


def test_module_executable_offers_help_without_read_env_nor_create_files(tmp_path: Path) -> None:
    process = subprocess.run(
        [sys.executable, "-m", "client_automation", "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert process.returncode == 0
    assert process.stdout == ""
    assert "--apply" in process.stderr
    assert list(tmp_path.iterdir()) == []
