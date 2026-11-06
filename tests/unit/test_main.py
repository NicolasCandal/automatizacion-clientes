"""Arguments, exit codes, and console logging without real data."""

import logging
import re
from os import environ
from pathlib import Path

import pytest
from pydantic_settings import SettingsError
from pytest_mock import MockerFixture

from client_automation import main as input
from client_automation.config import Config
from client_automation.core.errors import (
    AuthenticationError,
    AutomationError,
    ConcurrencyError,
    ConfigurationError,
    PersistenceError,
    RegistrationInterruptedError,
    StructureError,
)
from client_automation.infra.console_logging import console_logging


@pytest.fixture(autouse=True)
def isolate_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(environ):
        if name.upper().startswith("CA_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def configuration(tmp_path: Path, mocker: MockerFixture) -> Config:
    configuration = Config(base_directory=tmp_path, _env_file=None)
    mocker.patch.object(input, "load_config", return_value=configuration)
    return configuration


def test_help_no_load_configuration_nor_opens_inputs(
    mocker: MockerFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    load = mocker.patch.object(input, "load_config")
    run = mocker.patch.object(input, "run_excel")
    assert input.main(["--help"]) == 0
    load.assert_not_called()
    run.assert_not_called()
    output = capsys.readouterr()
    assert output.out == ""
    assert "--dry-run" in output.err
    assert "--apply" in output.err
    assert re.search("\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}Z INFO", output.err)


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["--week-header", " "],
        ["--unknown", "secret"],
        ["--week-header"],
        ["--week-header", "Week", "--dry-run", "--apply"],
        ["--no-env", "--env-file", "secret"],
    ],
)
def test_arguments_invalid_no_expose_their_values(
    arguments: list[str], mocker: MockerFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    load = mocker.patch.object(input, "load_config")
    assert input.main(arguments) == 2
    load.assert_not_called()
    output = capsys.readouterr()
    assert output.out == ""
    assert "secret" not in output.err
    assert "ERROR" in output.err


@pytest.mark.parametrize(
    ("option", "expected"), [((), None), (("--dry-run",), True), (("--apply",), False)]
)
def test_options_of_mode_and_header_forward(
    configuration: Config, mocker: MockerFixture, option: tuple[str, ...], expected: bool | None
) -> None:
    result = mocker.Mock(dry_run=True, registration=mocker.Mock(applied_sheets=()))
    run = mocker.patch.object(input, "run_excel", return_value=result)
    assert input.main(["--no-env", "--week-header", " Week ", *option]) == 0
    run.assert_called_once_with(
        configuration,
        header="Week",
        dry_run=expected,
        allow_pending=False,
        force_catalog=False,
    )


def test_env_and_base_explicit_resolve_without_expose_paths(
    configuration: Config, mocker: MockerFixture
) -> None:
    (configuration.base_directory / "options.env").write_text("", encoding="utf-8")
    load = mocker.patch.object(input, "load_config", return_value=configuration)
    run = mocker.patch.object(input, "run_excel", return_value=mocker.Mock(registration=None))
    assert (
        input.main(
            [
                "--env-file",
                "options.env",
                "--base-directory",
                str(configuration.base_directory),
                "--week-header",
                "Week",
                "--allow-pending",
                "--rebuild-catalog",
            ]
        )
        == 3
    )
    load.assert_called_once_with(Path("options.env"), base_directory=configuration.base_directory)
    assert run.call_args.kwargs["allow_pending"]
    assert run.call_args.kwargs["force_catalog"]


def test_env_file_explicit_missing_no_uses_values_by_default(
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    load = mocker.patch.object(input, "load_config")
    assert (
        input.main(
            [
                "--env-file",
                "missing.env",
                "--base-directory",
                str(tmp_path),
                "--week-header",
                "Week",
                "--apply",
            ]
        )
        == 2
    )
    load.assert_not_called()


@pytest.mark.parametrize(
    ("cause", "code"),
    [
        (ConfigurationError("secret"), 2),
        (StructureError("secret"), 2),
        (ConcurrencyError("secret"), 5),
        (PersistenceError("secret"), 4),
        (OSError("secret"), 4),
        (AutomationError("secret"), 1),
        (RuntimeError("secret"), 1),
        (KeyboardInterrupt(), 130),
        (
            RegistrationInterruptedError(
                backup=Path("path_secret.xlsx"),
                confirmed_sheets=("IOL",),
                current_sheet="Balanz",
            ),
            6,
        ),
    ],
)
def test_codes_and_messages_sanitized(
    configuration: Config,
    mocker: MockerFixture,
    capsys: pytest.CaptureFixture[str],
    cause: BaseException,
    code: int,
) -> None:
    mocker.patch.object(input, "run_excel", side_effect=cause)
    assert input.main(["--no-env", "--week-header", "Week"]) == code
    output = capsys.readouterr()
    assert output.out == ""
    assert "secret" not in output.err
    assert "path_secret" not in output.err
    assert "Traceback" not in output.err


def test_error_of_settings_no_exposes_the_value(
    mocker: MockerFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    mocker.patch.object(input, "load_config", side_effect=SettingsError("secret"))
    assert input.main(["--no-env", "--week-header", "Week"]) == 2
    assert "secret" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "field",
    ["catalog_path", "aliases_path", "resolution_path", "exclusion_list_path", "contacts_path"],
)
def test_paths_matching_reject_before_of_build_adapters(
    configuration: Config, mocker: MockerFixture, field: str
) -> None:
    configuration = configuration.model_copy(update={field: configuration.excel_path})
    reader = mocker.patch.object(input, "ExcelClientReader")
    with pytest.raises(ConfigurationError):
        input.run_excel(configuration, header="Week")
    reader.assert_not_called()


def test_paths_nested_and_backup_over_one_file_reject(configuration: Config) -> None:
    with pytest.raises(ConfigurationError):
        input.validate_paths(
            configuration.model_copy(
                update={"catalog_path": configuration.aliases_path / "other.json"}
            )
        )
    with pytest.raises(ConfigurationError):
        input.validate_paths(
            configuration.model_copy(update={"backup_directory": configuration.excel_path})
        )
    with pytest.raises(ConfigurationError):
        input.validate_paths(configuration, env_file=configuration.catalog_path)


def test_sheets_no_connects_on_repository_previous(
    configuration: Config, mocker: MockerFixture
) -> None:
    reader = mocker.patch.object(input, "ExcelClientReader")
    with pytest.raises(ConfigurationError):
        input.run_excel(configuration.model_copy(update={"repository": "sheets"}), header="Week")
    reader.assert_not_called()


def test_logging_restores_state_and_no_modifies_root(capsys: pytest.CaptureFixture[str]) -> None:
    package = logging.getLogger("client_automation")
    root = logging.getLogger()
    level, propagation, handlers = package.level, package.propagate, tuple(package.handlers)
    handlers_root = tuple(root.handlers)
    for _ in range(2):
        with console_logging() as console:
            console.info("Sample message.")
    assert package.level == level
    assert package.propagate == propagation
    assert tuple(package.handlers) == handlers
    assert tuple(root.handlers) == handlers_root
    assert capsys.readouterr().err.count("Sample message.") == 2


def test_logging_restores_even_if_fails_the_block() -> None:
    package = logging.getLogger("client_automation")
    handlers = tuple(package.handlers)
    with pytest.raises(RuntimeError), console_logging():
        raise RuntimeError("Test failure.")
    assert tuple(package.handlers) == handlers


@pytest.mark.parametrize(
    "options",
    [
        ["--week-header", "Week"],
        ["--dry-run"],
        ["--apply"],
        ["--allow-pending"],
        ["--rebuild-catalog"],
    ],
)
def test_authorization_no_accepts_options_of_registration(
    options: list[str],
    mocker: MockerFixture,
) -> None:
    load = mocker.patch.object(input, "load_config")
    authorize = mocker.patch.object(input, "authorize_google")
    assert input.main(["--authorize-google", *options]) == 2
    load.assert_not_called()
    authorize.assert_not_called()


def test_authorization_explicit_no_runs_flow(
    configuration: Config,
    mocker: MockerFixture,
) -> None:
    configuration = configuration.model_copy(
        update={"repository": "sheets", "spreadsheet_id": "document_synthetic"}
    )
    mocker.patch.object(input, "load_config", return_value=configuration)
    provider = mocker.patch.object(input, "GoogleCredentialsProvider")
    excel = mocker.patch.object(input, "run_excel")
    sheets = mocker.patch.object(input, "run_sheets")
    assert input.main(["--no-env", "--authorize-google"]) == 0
    provider.assert_called_once_with(configuration)
    provider.return_value.authorize.assert_called_once_with()
    provider.return_value.load.assert_not_called()
    excel.assert_not_called()
    sheets.assert_not_called()


def test_authorization_in_excel_rejects_before_of_open_credentials(
    configuration: Config,
    mocker: MockerFixture,
) -> None:
    provider = mocker.patch.object(input, "GoogleCredentialsProvider")
    assert input.main(["--no-env", "--authorize-google"]) == 2
    provider.assert_not_called()


def test_sheets_dispatches_with_the_options_of_command(
    configuration: Config,
    mocker: MockerFixture,
) -> None:
    configuration = configuration.model_copy(
        update={"repository": "sheets", "spreadsheet_id": "document_synthetic"}
    )
    mocker.patch.object(input, "load_config", return_value=configuration)
    excel = mocker.patch.object(input, "run_excel")
    sheets = mocker.patch.object(
        input, "run_sheets", return_value=mocker.Mock(dry_run=False, registration=None)
    )
    assert (
        input.main(
            [
                "--no-env",
                "--week-header",
                " Week ",
                "--apply",
                "--allow-pending",
                "--rebuild-catalog",
            ]
        )
        == 3
    )
    sheets.assert_called_once_with(
        configuration,
        header="Week",
        dry_run=False,
        allow_pending=True,
        force_catalog=True,
    )
    excel.assert_not_called()


@pytest.mark.parametrize("case", ["excel", "without_id", "without_header", "paths", "only_read"])
def test_preconditions_sheets_prevent_open_credentials(
    configuration: Config,
    mocker: MockerFixture,
    case: str,
) -> None:
    configuration = configuration.model_copy(
        update={
            "repository": "excel" if case == "excel" else "sheets",
            "spreadsheet_id": None if case == "without_id" else "document_synthetic",
        }
    )
    if case == "paths":
        configuration = configuration.model_copy(update={"token_path": configuration.contacts_path})
    provider = mocker.patch.object(input, "GoogleCredentialsProvider")
    with pytest.raises(ConfigurationError):
        input.run_sheets(
            configuration,
            header="" if case == "without_header" else "Week",
            dry_run=case != "only_read",
        )
    provider.assert_not_called()


def test_port_simulated_rejects_backups_and_marks_without_io(tmp_path: Path) -> None:
    writer = input._DryRunWriter()
    with pytest.raises(ConfigurationError):
        writer.create_backup()
    with pytest.raises(ConfigurationError):
        writer.apply_marks("IOL", 3, "Week", frozenset(), expected_revision="revision")
    assert list(tmp_path.iterdir()) == []


def test_failure_of_authorization_reports_operation_explicit_without_secrets(
    configuration: Config,
    mocker: MockerFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mocker.patch.object(input, "run_excel", side_effect=AuthenticationError("secret"))
    assert input.main(["--no-env", "--week-header", "Week"]) == 4
    output = capsys.readouterr()
    assert "--authorize-google" in output.err
    assert "secret" not in output.err
