"""Import CLI options without real chat files."""

from datetime import date
from os import environ
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from client_automation import main as input
from client_automation.config import Config
from client_automation.core.errors import ConfigurationError
from client_automation.core.models import WeeklyPeriod

PERIOD = WeeklyPeriod(start_date=date(2026, 11, 2), end_date=date(2026, 11, 6))


@pytest.fixture(autouse=True)
def isolate_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(environ):
        if name.upper().startswith("CA_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)


def arguments(*options: str) -> list[str]:
    return [
        "--no-env",
        "--import-whatsapp",
        "--start-date",
        "2026-11-02",
        "--end-date",
        "2026-11-06",
        *options,
    ]


@pytest.mark.parametrize(
    "options",
    [
        ["--import-whatsapp"],
        ["--import-whatsapp", "--start-date", "2026-11-02"],
        ["--import-whatsapp", "--end-date", "2026-11-06"],
        ["--import-whatsapp", "--start-date", "2026-11-07", "--end-date", "2026-11-02"],
        ["--import-whatsapp", "--start-date", "2026-11-31", "--end-date", "2026-12-01"],
        ["--import-whatsapp", "--start-date", "20261102", "--end-date", "2026-11-06"],
        ["--import-whatsapp", "--start-date", "2026-W45-1", "--end-date", "2026-11-06"],
        ["--import-whatsapp", "--start-date", "secret", "--end-date", "2026-11-06"],
        ["--import-whatsapp", "--authorize-google"],
        ["--week-header", "Week", "--start-date", "2026-11-02"],
        ["--authorize-google", "--end-date", "2026-11-06"],
        ["--week-header", "Week", "--manifest", "secret.json"],
        ["--authorize-google", "--manifest", "secret.json"],
        ["--manifest", "secret.json"],
    ],
)
def test_options_incompatible_reject_before_of_load_configuration(
    options: list[str],
    mocker: MockerFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    load = mocker.patch.object(input, "load_config")
    import_contacts = mocker.patch.object(input, "run_import_operation")
    assert input.main(options) == 2
    load.assert_not_called()
    import_contacts.assert_not_called()
    output = capsys.readouterr()
    assert output.out == ""
    assert "secret" not in output.err
    assert "Traceback" not in output.err


@pytest.mark.parametrize(
    "options",
    [
        ["--week-header", "Week"],
        ["--allow-pending"],
        ["--rebuild-catalog"],
    ],
)
def test_import_operation_no_accepts_options_of_registration(
    options: list[str],
    mocker: MockerFixture,
) -> None:
    load = mocker.patch.object(input, "load_config")
    assert input.main(arguments(*options)) == 2
    load.assert_not_called()


@pytest.mark.parametrize(
    ("option", "expected"), [((), None), (("--dry-run",), True), (("--apply",), False)]
)
def test_command_forwards_period_and_mode_without_run_registration(
    tmp_path: Path,
    mocker: MockerFixture,
    option: tuple[str, ...],
    expected: bool | None,
) -> None:
    configuration = Config(base_directory=tmp_path, _env_file=None)
    mocker.patch.object(input, "load_config", return_value=configuration)
    result = mocker.Mock(
        dry_run=True,
        extraction=mocker.Mock(total_chats_scanned=2, week=mocker.Mock(contacts=())),
    )
    import_contacts = mocker.patch.object(input, "run_import_operation", return_value=result)
    excel = mocker.patch.object(input, "run_excel")
    sheets = mocker.patch.object(input, "run_sheets")
    authorize = mocker.patch.object(input, "authorize_google")
    assert input.main(arguments(*option)) == 0
    import_contacts.assert_called_once_with(configuration, period=PERIOD, dry_run=expected)
    excel.assert_not_called()
    sheets.assert_not_called()
    authorize.assert_not_called()


@pytest.mark.parametrize("absolute", [False, True])
def test_manifest_explicit_resolves_start_date_base_configured(
    tmp_path: Path,
    mocker: MockerFixture,
    absolute: bool,
) -> None:
    base = tmp_path / "base_configured"
    configuration = Config(base_directory=base, _env_file=None)
    mocker.patch.object(input, "load_config", return_value=configuration)
    import_contacts = mocker.patch.object(input, "run_import_operation")
    import_contacts.return_value.extraction.week.contacts = ()
    path = base / "batch.json" if absolute else Path("batch.json")
    assert input.main(arguments("--manifest", str(path))) == 0
    received = import_contacts.call_args.args[0]
    assert received.whatsapp_manifest_path == base / "batch.json"
    assert configuration.whatsapp_manifest_path != received.whatsapp_manifest_path
    assert not base.exists()


@pytest.mark.parametrize("field", ["token_path", "credentials_path", "contacts_path", "excel_path"])
def test_destination_no_can_occupy_files_of_input_or_secrets(
    tmp_path: Path,
    mocker: MockerFixture,
    field: str,
) -> None:
    configuration = Config(base_directory=tmp_path, _env_file=None)
    configuration = configuration.model_copy(
        update={"contacts_path": getattr(configuration, field)}
    )
    if field == "contacts_path":
        configuration = configuration.model_copy(
            update={"contacts_path": configuration.whatsapp_manifest_path}
        )
    extractor = mocker.patch.object(input, "WhatsAppTxtExtractor")
    with pytest.raises(ConfigurationError):
        input.run_import_operation(configuration, period=PERIOD, dry_run=False)
    extractor.assert_not_called()


def test_help_includes_import_operation_without_read_configuration(
    mocker: MockerFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    load = mocker.patch.object(input, "load_config")
    assert input.main(["--help"]) == 0
    load.assert_not_called()
    help = capsys.readouterr().err
    for option in ("--import-whatsapp", "--start-date", "--end-date", "--manifest"):
        assert option in help
