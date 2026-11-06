"""Sheets CLI with the real SDK, mocked HTTP, and temporary synthetic credentials."""

import json
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from os import environ
from pathlib import Path
from typing import cast

import pytest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from pytest_mock import MockerFixture
from requests import PreparedRequest, Response
from requests.adapters import HTTPAdapter
from requests.exceptions import Timeout

from client_automation import main as input
from client_automation.config import Config
from client_automation.core.errors import AuthorizationError, PersistenceError
from client_automation.core.models import WeeklyResolution
from client_automation.infra.google_authentication import GoogleCredentialsProvider
from client_automation.infra.json_alias_store import JsonAliasStore
from client_automation.infra.json_catalog_store import JsonCatalogStore
from client_automation.infra.json_contact_reader import JsonContactReader
from client_automation.infra.json_resolution_store import JsonResolutionStore
from client_automation.infra.json_storage import save_json
from client_automation.infra.sheets_client_reader import (
    SheetsClientReader,
    create_reader_sheets,
)
from client_automation.infra.sheets_client_writer import SheetsClientWriter

SCOPE = "https://www.googleapis.com/auth/spreadsheets"
READ_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
type Object = dict[str, object]


def require_object(value: object) -> Object:
    return cast(Object, value)


def require_list(value: object) -> list[Object]:
    return cast(list[Object], value)


def sheet(document: Object, name: str) -> Object:
    return next(
        destination
        for destination in require_list(document["sheets"])
        if require_object(destination["properties"])["title"] == name
    )


def cell(destination: Object, row: int, column: int) -> Object:
    rows = require_list(require_list(destination["data"])[0]["rowData"])
    while len(rows) < row:
        rows.append({"values": []})
    values = require_list(rows[row - 1]["values"])
    while len(values) < column:
        values.append({})
    return values[column - 1]


def response(document: object, state: int = 200) -> Response:
    result = Response()
    result.status_code = state
    result.encoding = "utf-8"
    result._content = json.dumps(document).encode("utf-8")
    _ = result.content
    return result


class FakeApi:
    def __init__(self) -> None:
        self.document: Object = {"spreadsheetId": "document_synthetic", "sheets": []}
        self.batches: list[Object] = []
        self.queries = 0
        self.refreshes = 0
        self.scope = SCOPE
        self.fail_after_sheet: str | None = None
        self.state_read = 200
        for catalog_index, name in enumerate(("Inviu", "IOL", "Bull", "Balanz", "PPI")):
            destination: Object = {
                "properties": {
                    "title": name,
                    "sheetId": catalog_index,
                    "gridProperties": {"rowCount": 100, "columnCount": 8},
                },
                "data": [{"rowData": []}],
            }
            cell(destination, 1, 1)["userEnteredValue"] = {"stringValue": "Client"}
            cell(destination, 2, 4 if name == "Balanz" else 3)["userEnteredValue"] = {
                "stringValue": "Previous"
            }
            cell(destination, 3, 1)["userEnteredValue"] = {"stringValue": "Axa Sample"}
            cell(destination, 5, 1)["userEnteredValue"] = {"stringValue": "Notuka Example"}
            cell(destination, 3, 2)["userEnteredValue"] = {"formulaValue": "=100+200"}
            if name == "IOL":
                cell(destination, 8, 1)["userEnteredValue"] = {
                    "stringValue": "AXA SAMPLE AND/OR NOTUKA EXAMPLE"
                }
            require_list(self.document["sheets"]).append(destination)

    def send(self, request: PreparedRequest, **options: object) -> Response:
        assert options["timeout"] is not None
        if request.url == "https://oauth2.googleapis.com/token":
            assert request.method == "POST"
            self.refreshes += 1
            return response(
                {
                    "access_token": "access_refreshed_fake",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                    "scope": self.scope,
                }
            )
        assert request.url is not None
        assert request.url.startswith(
            "https://sheets.googleapis.com/v4/spreadsheets/document_synthetic"
        )
        assert request.headers["authorization"] in {
            "Bearer access_fake",
            "Bearer access_refreshed_fake",
        }
        if request.method == "GET":
            self.queries += 1
            return response(
                self.document if self.state_read == 200 else {"error": "fake"},
                self.state_read,
            )
        assert request.method == "POST"
        assert request.url.endswith(":batchUpdate")
        body = request.body
        assert isinstance(body, (str, bytes))
        content = require_object(json.loads(body))
        self.batches.append(content)
        name = ""
        for batch_request in require_list(content["requests"]):
            update = require_object(batch_request["updateCells"])
            start = require_object(update["start"])
            destination = next(
                destination
                for destination in require_list(self.document["sheets"])
                if require_object(destination["properties"])["sheetId"] == start["sheetId"]
            )
            name = cast(str, require_object(destination["properties"])["title"])
            for offset, row in enumerate(require_list(update["rows"]), 1):
                value = require_list(row["values"])[0]
                current = cell(
                    destination,
                    cast(int, start["rowIndex"]) + offset,
                    cast(int, start["columnIndex"]) + 1,
                )
                current.pop("userEnteredValue", None)
                current.pop("effectiveValue", None)
                if "userEnteredValue" in value:
                    current["userEnteredValue"] = deepcopy(value["userEnteredValue"])
                if "userEnteredFormat" in value:
                    format = require_object(current.setdefault("userEnteredFormat", {}))
                    format.update(deepcopy(require_object(value["userEnteredFormat"])))
                    current["effectiveFormat"] = deepcopy(format)
        if self.fail_after_sheet == name:
            raise Timeout("synthetic failure after applying")
        return response({"spreadsheetId": "document_synthetic"})


@dataclass
class Scenario:
    configuration: Config
    api: FakeApi

    def arguments(self, *options: str) -> list[str]:
        return [
            "--no-env",
            "--base-directory",
            str(self.configuration.base_directory),
            "--week-header",
            "2026-11-02 / 2026-11-06",
            *options,
        ]


@pytest.fixture(autouse=True)
def isolate_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture
) -> None:
    for name in tuple(environ):
        if name.upper().startswith("CA_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    mocker.patch("requests.sessions.get_netrc_auth", return_value=None)
    mocker.patch(
        "webbrowser.open", side_effect=AssertionError("Browser access is forbidden in this test")
    )


def write_contacts(configuration: Config, name: str = "Axa Sample (IOL/Balanz)") -> None:
    save_json(
        configuration.contacts_path,
        {
            "period": {"start_date": "2026-11-02", "end_date": "2026-11-06"},
            "contacts": [{"raw_name": name, "last_activity_date": "2026-11-05"}],
        },
    )


def write_token(configuration: Config, *, expired: bool = False, scope: str = SCOPE) -> None:
    save_json(
        configuration.token_path,
        {
            "token": "access_fake",
            "refresh_token": "refresh_fake",
            "client_id": "client_fake",
            "client_secret": "secret_fake",
            "token_uri": "https://oauth2.googleapis.com/token",
            "scopes": [scope],
            "expiry": "2000-01-01T00:00:00Z" if expired else "2099-01-01T00:00:00Z",
        },
    )


def files(base: Path) -> dict[Path, bytes]:
    return {path.relative_to(base): path.read_bytes() for path in base.rglob("*") if path.is_file()}


@pytest.fixture
def scenario(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture) -> Scenario:
    monkeypatch.setenv("CA_REPOSITORY", "sheets")
    monkeypatch.setenv("CA_SPREADSHEET_ID", "document_synthetic")
    monkeypatch.setenv("CA_GOOGLE_SCOPES", json.dumps([SCOPE]))
    monkeypatch.setenv("CA_RETRY_MIN_WAIT_SECONDS", "0.001")
    monkeypatch.setenv("CA_RETRY_MAX_WAIT_SECONDS", "0.002")
    configuration = Config(base_directory=tmp_path, _env_file=None)
    write_contacts(configuration)
    write_token(configuration)
    api = FakeApi()
    mocker.patch.object(HTTPAdapter, "send", side_effect=api.send)
    return Scenario(configuration, api)


@pytest.mark.parametrize("expired", [False, True])
def test_dry_run_no_saves_files_nor_builds_writer(
    scenario: Scenario,
    mocker: MockerFixture,
    capsys: pytest.CaptureFixture[str],
    expired: bool,
) -> None:
    configuration = scenario.configuration
    write_token(configuration, expired=expired)
    original = files(configuration.base_directory)
    document = deepcopy(scenario.api.document)
    factory = mocker.patch.object(input, "create_writer_sheets")
    assert input.main(scenario.arguments()) == 0
    factory.assert_not_called()
    assert files(configuration.base_directory) == original
    assert scenario.api.document == document
    assert not scenario.api.batches
    assert scenario.api.refreshes == int(expired)
    output = capsys.readouterr()
    assert output.out == ""
    assert "Sheet plan: IOL; column=4" in output.err
    for secret in (
        "Axa Sample",
        "secret_fake",
        "access_fake",
        "document_synthetic",
        str(configuration.token_path),
    ):
        assert secret not in output.err


def test_apply_connects_all_and_repeat_no_creates_other_backup(
    scenario: Scenario,
    mocker: MockerFixture,
) -> None:
    configuration = scenario.configuration
    original = deepcopy(scenario.api.document)
    read_contacts = mocker.spy(JsonContactReader, "read")
    assert input.main(scenario.arguments("--apply")) == 0
    read_contacts.assert_called_once()
    backups = list(configuration.backup_directory.iterdir())
    assert len(backups) == 1
    assert (
        require_object(json.loads(backups[0].read_text(encoding="utf-8")))["document"] == original
    )
    catalog = JsonCatalogStore(configuration.catalog_path).read()
    assert catalog is not None
    assert catalog.source_id == "sheets:document_synthetic"
    assert len(JsonResolutionStore(configuration.resolution_path).read().resolved) == 1
    assert (
        JsonAliasStore(configuration.aliases_path).find_by_name("Axa Sample (IOL/Balanz)")
        is not None
    )
    credentials = GoogleCredentialsProvider(configuration).load()
    with create_reader_sheets(configuration, credentials) as reader:
        assert reader.read_marks("IOL", 4) == frozenset({3, 8})
        assert reader.read_marks("Balanz", 5) == frozenset({3})
        assert reader.read_marks("Bull", 4) == frozenset()
    assert cell(sheet(scenario.api.document, "IOL"), 3, 2) == cell(sheet(original, "IOL"), 3, 2)
    assert len(scenario.api.batches) == 5
    assert input.main(scenario.arguments("--apply")) == 0
    assert len(scenario.api.batches) == 5
    assert list(configuration.backup_directory.iterdir()) == backups


def test_token_refreshed_saves_in_mode_real(scenario: Scenario) -> None:
    write_token(scenario.configuration, expired=True)
    assert input.main(scenario.arguments("--apply")) == 0
    assert scenario.api.refreshes == 1
    token = require_object(
        json.loads(scenario.configuration.token_path.read_text(encoding="utf-8"))
    )
    assert token["token"] == "access_refreshed_fake"


@pytest.mark.parametrize("apply", [False, True])
def test_only_read_dry_runs_and_rejects_apply_before_of_authenticate(
    scenario: Scenario,
    monkeypatch: pytest.MonkeyPatch,
    mocker: MockerFixture,
    apply: bool,
) -> None:
    monkeypatch.setenv("CA_GOOGLE_SCOPES", json.dumps([READ_SCOPE]))
    write_token(scenario.configuration, scope=READ_SCOPE)
    load = mocker.spy(GoogleCredentialsProvider, "load")
    original = files(scenario.configuration.base_directory)
    assert input.main(scenario.arguments(*(["--apply"] if apply else []))) == (2 if apply else 0)
    assert load.call_count == (0 if apply else 1)
    assert not scenario.api.batches
    assert files(scenario.configuration.base_directory) == original


@pytest.mark.parametrize("apply", [False, True])
def test_pending_block_marks_and_in_real_save_report(
    scenario: Scenario,
    apply: bool,
) -> None:
    configuration = scenario.configuration
    write_contacts(configuration, "Unknown identity zzz 987")
    assert input.main(scenario.arguments(*(["--apply"] if apply else []))) == 3
    assert not scenario.api.batches
    assert not configuration.backup_directory.exists()
    assert not configuration.aliases_path.exists()
    assert configuration.catalog_path.exists() is apply
    assert configuration.resolution_path.exists() is apply


def test_allow_pending_applies_only_resolved(scenario: Scenario) -> None:
    write_contacts(scenario.configuration, "Unknown identity zzz 987")
    assert input.main(scenario.arguments("--apply", "--allow-pending")) == 0
    assert len(scenario.api.batches) == 5
    assert not scenario.configuration.aliases_path.exists()


@pytest.mark.parametrize("dry_run", [False, True])
def test_mode_of_environment_and_override_respect(
    scenario: Scenario,
    monkeypatch: pytest.MonkeyPatch,
    dry_run: bool,
) -> None:
    monkeypatch.setenv("CA_DRY_RUN", "false")
    assert input.main(scenario.arguments(*(["--dry-run"] if dry_run else []))) == 0
    assert bool(scenario.api.batches) is not dry_run


def test_token_missing_no_starts_oauth_automatically(
    scenario: Scenario,
    mocker: MockerFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scenario.configuration.token_path.unlink()
    authorize = mocker.spy(GoogleCredentialsProvider, "authorize")
    assert input.main(scenario.arguments()) == 4
    authorize.assert_not_called()
    assert scenario.api.queries == 0
    assert "--authorize-google" in capsys.readouterr().err


def test_failure_of_contacts_closes_reader_and_writer(
    scenario: Scenario,
    mocker: MockerFixture,
) -> None:
    scenario.configuration.contacts_path.write_bytes(b"{invalido")
    reader = mocker.spy(SheetsClientReader, "close")
    writer = mocker.spy(SheetsClientWriter, "close")
    assert input.main(scenario.arguments("--apply")) == 2
    writer.assert_called_once()
    assert reader.call_count == 2  # The workflow reader and the writer's internal reader.
    assert not scenario.api.batches


def test_failure_of_report_prevents_backups_and_marks(
    scenario: Scenario,
    mocker: MockerFixture,
) -> None:
    mocker.patch.object(JsonResolutionStore, "save", side_effect=PersistenceError("fake"))
    assert input.main(scenario.arguments("--apply")) == 4
    assert not scenario.api.batches
    assert not scenario.configuration.backup_directory.exists()


def test_change_after_save_report_stops_registration(
    scenario: Scenario,
    mocker: MockerFixture,
) -> None:
    save = JsonResolutionStore.save

    def save_and_change(self: JsonResolutionStore, resolution: WeeklyResolution) -> None:
        save(self, resolution)
        cell(sheet(scenario.api.document, "IOL"), 3, 1)["userEnteredValue"] = {
            "stringValue": "Other Person"
        }

    mocker.patch.object(JsonResolutionStore, "save", autospec=True, side_effect=save_and_change)
    assert input.main(scenario.arguments("--apply")) == 5
    assert not scenario.api.batches
    assert not scenario.configuration.backup_directory.exists()


def test_response_lost_after_of_apply_returns_six_and_preserves_backup(
    scenario: Scenario,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scenario.api.fail_after_sheet = "IOL"
    assert input.main(scenario.arguments("--apply")) == 6
    assert len(scenario.api.batches) == 2
    assert len(list(scenario.configuration.backup_directory.iterdir())) == 1
    output = capsys.readouterr()
    assert "1 sheets confirmed" in output.err
    assert "sheet in progress=True" in output.err
    assert "Traceback" not in output.err


@pytest.mark.parametrize("state", [401, 403, 404])
def test_error_http_translates_without_retries_of_permission(
    scenario: Scenario,
    state: int,
) -> None:
    scenario.api.state_read = state
    assert input.main(scenario.arguments()) == 4
    assert scenario.api.queries == 1
    assert not scenario.api.batches


def test_cache_invalid_repairs_only_with_option_explicit(scenario: Scenario) -> None:
    configuration = scenario.configuration
    configuration.catalog_path.parent.mkdir(parents=True)
    configuration.catalog_path.write_bytes(b"{invalido")
    assert input.main(scenario.arguments()) == 2
    assert input.main(scenario.arguments("--rebuild-catalog")) == 0
    assert configuration.catalog_path.read_bytes() == b"{invalido"
    assert input.main(scenario.arguments("--rebuild-catalog", "--apply")) == 0
    assert JsonCatalogStore(configuration.catalog_path).read() is not None


def test_authorization_command_saves_token_without_run_sheets(
    scenario: Scenario,
    mocker: MockerFixture,
) -> None:
    configuration = scenario.configuration
    save_json(
        configuration.credentials_path,
        {
            "installed": {
                "auth_uri": "https://accounts.google.com/o/oauth2/v2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_id": "client_fake",
                "client_secret": "secret_fake",
                "redirect_uris": ["http://localhost"],
            }
        },
    )
    make = cast(
        Callable[[Mapping[str, object]], Credentials], Credentials.from_authorized_user_info
    )
    credentials = make(
        require_object(json.loads(configuration.token_path.read_text(encoding="utf-8")))
    )
    flow = mocker.Mock()
    flow.run_local_server.return_value = credentials
    mocker.patch.object(InstalledAppFlow, "from_client_config", return_value=flow)
    assert (
        input.main(
            [
                "--no-env",
                "--base-directory",
                str(configuration.base_directory),
                "--authorize-google",
            ]
        )
        == 0
    )
    flow.run_local_server.assert_called_once()
    flow.oauth2session.close.assert_called_once()
    assert scenario.api.queries == 0
    assert not scenario.api.batches
    assert not configuration.resolution_path.exists()


def test_failure_on_build_writer_closes_reader_previous(
    scenario: Scenario,
    mocker: MockerFixture,
) -> None:
    closure = mocker.spy(SheetsClientReader, "close")
    mocker.patch.object(input, "create_writer_sheets", side_effect=AuthorizationError("fake"))
    assert input.main(scenario.arguments("--apply")) == 4
    closure.assert_called_once()
    assert scenario.api.queries == 0
    assert not scenario.api.batches


def test_authorization_interactive_no_applies_to_account_service(
    scenario: Scenario,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CA_GOOGLE_AUTHENTICATION_TYPE", "service_account")
    original = files(scenario.configuration.base_directory)
    assert (
        input.main(
            [
                "--no-env",
                "--base-directory",
                str(scenario.configuration.base_directory),
                "--authorize-google",
            ]
        )
        == 2
    )
    assert files(scenario.configuration.base_directory) == original
    assert scenario.api.queries == 0
