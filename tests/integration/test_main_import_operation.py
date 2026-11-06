"""Full import CLI with temporary, synthetic TXT and JSON files."""

import json
import subprocess
import sys
from collections.abc import Iterable, Iterator
from datetime import date
from os import environ
from pathlib import Path
from typing import cast

import pytest
from pytest_mock import MockerFixture

from client_automation import main as input
from client_automation.config import Config
from client_automation.core.models import WeeklyPeriod
from client_automation.core.whatsapp_import import (
    ChatActivity,
    DateOrder,
    parse_activity,
)
from client_automation.infra import json_storage, whatsapp_txt_extractor
from client_automation.infra.json_contact_reader import JsonContactReader


@pytest.fixture(autouse=True)
def isolate_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture
) -> Iterator[None]:
    for name in tuple(environ):
        if name.upper().startswith("CA_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    operations = [
        mocker.patch.object(
            input, name, side_effect=AssertionError("Operation unrelated to import")
        )
        for name in (
            "GoogleCredentialsProvider",
            "ExcelClientReader",
            "create_reader_sheets",
            "create_writer_sheets",
            "run_excel",
            "run_sheets",
        )
    ]
    mocker.patch(
        "requests.sessions.Session.send", side_effect=AssertionError("Network access is forbidden")
    )
    mocker.patch("webbrowser.open", side_effect=AssertionError("Browser access is forbidden"))
    yield
    for operation in operations:
        operation.assert_not_called()


@pytest.fixture
def configuration(tmp_path: Path) -> Config:
    configuration = Config(base_directory=tmp_path, _env_file=None)
    folder = configuration.whatsapp_manifest_path.parent
    folder.mkdir(parents=True)
    (folder / "axa.txt").write_text(
        "02/11/2026, 10:00 - Axa: private synthetic content\n"
        "05/11/2026, 10:00 - Me: another private synthetic line\n"
        "10/11/2026, 10:00 - Axa: later message\n",
        encoding="utf-8",
    )
    (folder / "group.txt").write_text(
        "[03/11/2026, 10:00:30] Me: synthetic group message\n", encoding="utf-8"
    )
    document = {
        "schema_version": 1,
        "date_order": "day_month",
        "chats": [
            {
                "file": "axa.txt",
                "raw_name": "Axa Sample (IOL)",
                "phone": "+5491100000000",
                "is_group": False,
                "senders": ["Me", "Axa"],
            },
            {
                "file": "group.txt",
                "raw_name": "Group of example",
                "is_group": True,
                "senders": ["Me"],
            },
        ],
    }
    configuration.whatsapp_manifest_path.write_text(json.dumps(document), encoding="utf-8")
    return configuration


def arguments(configuration: Config, *options: str) -> list[str]:
    return [
        "--no-env",
        "--base-directory",
        str(configuration.base_directory),
        "--import-whatsapp",
        "--start-date",
        "2026-11-02",
        "--end-date",
        "2026-11-06",
        *options,
    ]


def files(base: Path) -> dict[Path, bytes]:
    return {path.relative_to(base): path.read_bytes() for path in base.rglob("*") if path.is_file()}


def test_dry_run_default_no_saves_nor_leaks_data_in_logs(
    configuration: Config,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original = files(configuration.base_directory)
    assert input.main(arguments(configuration)) == 0
    assert files(configuration.base_directory) == original
    output = capsys.readouterr()
    assert output.out == ""
    assert "dry_run=True; 2 chats; 2 contacts" in output.err
    for secret in (
        "Axa Sample",
        "+54911",
        "content fake",
        str(configuration.whatsapp_manifest_path),
    ):
        assert secret not in output.err


def test_apply_generates_only_input_weekly_and_preserves_sources(configuration: Config) -> None:
    configuration.contacts_path.write_bytes(b"salida anterior")
    original = files(configuration.base_directory)
    assert input.main(arguments(configuration, "--apply")) == 0
    week = JsonContactReader(configuration.contacts_path).read()
    assert week.period == WeeklyPeriod(start_date=date(2026, 11, 2), end_date=date(2026, 11, 6))
    assert week.contacts[0].last_activity_date == date(2026, 11, 5)
    assert week.contacts[1].is_group
    assert "content fake" not in configuration.contacts_path.read_text(encoding="utf-8")
    current = files(configuration.base_directory)
    key = configuration.contacts_path.relative_to(configuration.base_directory)
    current.pop(key)
    original.pop(key)
    assert current == original
    assert not configuration.backup_directory.exists()
    assert not configuration.resolution_path.exists()


@pytest.mark.parametrize("dry_run", [False, True])
def test_mode_of_environment_and_override_respect(
    configuration: Config,
    monkeypatch: pytest.MonkeyPatch,
    dry_run: bool,
) -> None:
    monkeypatch.setenv("CA_DRY_RUN", "false")
    assert input.main(arguments(configuration, *(["--dry-run"] if dry_run else []))) == 0
    assert configuration.contacts_path.exists() is not dry_run


def test_repository_sheets_no_requires_credentials_for_import_contacts(
    configuration: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CA_REPOSITORY", "sheets")
    monkeypatch.setenv("CA_SPREADSHEET_ID", "document_fake")
    assert input.main(arguments(configuration, "--apply")) == 0
    assert configuration.contacts_path.exists()
    assert not configuration.token_path.exists()
    assert not configuration.credentials_path.exists()


def test_manifest_by_argument_overrides_over_environment(
    configuration: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = configuration.whatsapp_manifest_path
    monkeypatch.setenv("CA_WHATSAPP_MANIFEST_PATH", "missing.json")
    relative = path.relative_to(configuration.base_directory)
    assert input.main(arguments(configuration, "--manifest", str(relative), "--apply")) == 0
    assert configuration.contacts_path.exists()


def test_file_of_environment_configures_manifest_and_destination(configuration: Config) -> None:
    path_env = configuration.base_directory / "options.env"
    path_env.write_text("CA_CONTACTS_PATH=output/week.json\nCA_DRY_RUN=false\n", encoding="utf-8")
    assert (
        input.main(
            [
                "--env-file",
                "options.env",
                "--base-directory",
                str(configuration.base_directory),
                "--import-whatsapp",
                "--start-date",
                "2026-11-02",
                "--end-date",
                "2026-11-06",
            ]
        )
        == 0
    )
    assert (configuration.base_directory / "output/week.json").exists()
    assert not configuration.contacts_path.exists()


def test_one_error_of_batch_preserves_output_previous(configuration: Config) -> None:
    configuration.contacts_path.write_bytes(b"salida anterior")
    (configuration.whatsapp_manifest_path.parent / "group.txt").write_bytes(b"\xff")
    original = files(configuration.base_directory)
    assert input.main(arguments(configuration, "--apply")) == 2
    assert files(configuration.base_directory) == original


def test_manifest_missing_returns_four_without_create_output(configuration: Config) -> None:
    assert input.main(arguments(configuration, "--manifest", "missing.json", "--apply")) == 4
    assert not configuration.contacts_path.exists()


def test_change_of_source_returns_five_without_save(
    configuration: Config,
    mocker: MockerFixture,
) -> None:
    def parse_and_change(
        lines: Iterable[str],
        *,
        period: WeeklyPeriod,
        senders: tuple[str, ...],
        date_order: DateOrder,
        short_year_century: int | None = None,
    ) -> ChatActivity:
        result = parse_activity(
            lines,
            period=period,
            senders=senders,
            date_order=date_order,
            short_year_century=short_year_century,
        )
        path = configuration.whatsapp_manifest_path.parent / "axa.txt"
        path.write_bytes(path.read_bytes() + b"\n")
        return result

    mocker.patch.object(whatsapp_txt_extractor, "parse_activity", side_effect=parse_and_change)
    assert input.main(arguments(configuration, "--apply")) == 5
    assert not configuration.contacts_path.exists()


def test_failure_of_replacement_preserves_output_and_cleans_temporary(
    configuration: Config,
    mocker: MockerFixture,
) -> None:
    configuration.contacts_path.write_bytes(b"salida anterior")
    original = files(configuration.base_directory)
    mocker.patch.object(json_storage, "_replace_temporary", side_effect=OSError("secret"))
    assert input.main(arguments(configuration, "--apply")) == 4
    assert files(configuration.base_directory) == original


@pytest.mark.parametrize("field", ["whatsapp_manifest_path", "token_path", "credentials_path"])
def test_paths_matching_reject_without_alter_inputs(
    configuration: Config,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    path = cast(Path, getattr(configuration, field))
    monkeypatch.setenv("CA_CONTACTS_PATH", str(path))
    original = files(configuration.base_directory)
    assert input.main(arguments(configuration, "--apply")) == 2
    assert files(configuration.base_directory) == original


def test_no_can_use_file_env_as_manifest(configuration: Config) -> None:
    path_env = configuration.base_directory / "options.env"
    path_env.write_text("CA_DRY_RUN=true\n", encoding="utf-8")
    original = path_env.read_bytes()
    assert (
        input.main(
            [
                "--env-file",
                "options.env",
                "--base-directory",
                str(configuration.base_directory),
                "--import-whatsapp",
                "--start-date",
                "2026-11-02",
                "--end-date",
                "2026-11-06",
                "--manifest",
                "options.env",
            ]
        )
        == 2
    )
    assert path_env.read_bytes() == original


def test_without_activity_generates_one_week_empty_validates(configuration: Config) -> None:
    options = arguments(configuration, "--apply")
    options[options.index("2026-11-02")] = "2027-01-01"
    options[options.index("2026-11-06")] = "2027-01-07"
    assert input.main(options) == 0
    assert JsonContactReader(configuration.contacts_path).read().contacts == ()


def test_module_executable_imports_in_subprocess_without_files_real(configuration: Config) -> None:
    process = subprocess.run(
        [sys.executable, "-m", "client_automation", *arguments(configuration, "--apply")],
        cwd=configuration.base_directory,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert process.returncode == 0
    assert process.stdout == ""
    assert len(JsonContactReader(configuration.contacts_path).read().contacts) == 2
    assert not configuration.backup_directory.exists()
