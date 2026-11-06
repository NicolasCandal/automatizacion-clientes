"""Regression tests for sheets client writer."""

import json
from collections.abc import Callable, Mapping
from copy import deepcopy
from datetime import UTC, date, datetime
from os import environ
from pathlib import Path
from typing import cast

import pytest
from google.auth.credentials import AnonymousCredentials
from google.auth.exceptions import GoogleAuthError, RefreshError
from google.oauth2.credentials import Credentials
from pytest_mock import MockerFixture
from requests import Response, Session
from requests.exceptions import ConnectionError, RequestException, SSLError, Timeout

from client_automation.config import Config
from client_automation.core.catalog import build_catalog
from client_automation.core.contracts import ClientWriter
from client_automation.core.errors import (
    AuthenticationError,
    AuthorizationError,
    ConcurrencyError,
    ConfigurationError,
    ExternalServiceError,
    OperationTimeoutError,
    PersistenceError,
    RegistrationInterruptedError,
    StructureError,
    TransientError,
)
from client_automation.core.models import (
    WeeklyContacts,
    WeeklyPeriod,
    WeeklyResolution,
)
from client_automation.infra import sheets_client_writer as module
from client_automation.infra.sheets_client_reader import SheetsClientReader
from client_automation.infra.sheets_client_writer import (
    SheetsClientWriter,
    create_writer_sheets,
)
from client_automation.services.registration import RegistrationService

type Object = dict[str, object]


class FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 11, 6, 12, tzinfo=UTC)


class FakeResponse(Response):
    def __init__(self, content: object, state: int = 200) -> None:
        super().__init__()
        self.status_code = state
        self.payload = content
        self.closures = 0

    def json(self, **kwargs: object) -> object:
        if isinstance(self.payload, Exception):
            raise self.payload
        return deepcopy(self.payload)

    def close(self) -> None:
        self.closures += 1


def require_object(value: object) -> Object:
    return cast(Object, value)


def require_list(value: object) -> list[Object]:
    return cast(list[Object], value)


def sheet(document: Object, name: str = "IOL") -> Object:
    return next(
        destination
        for destination in require_list(document["sheets"])
        if require_object(destination["properties"])["title"] == name
    )


def cells(destination: Object) -> list[Object]:
    return require_list(require_list(destination["data"])[0]["rowData"])


def cell(destination: Object, row: int, column: int) -> Object:
    rows = cells(destination)
    while len(rows) < row:
        rows.append({"values": []})
    values = require_list(rows[row - 1]["values"])
    while len(values) < column:
        values.append({})
    return values[column - 1]


class FakeTransport:
    def __init__(self, document: Object) -> None:
        self.document = document
        self.sends: list[Object] = []
        self.queries = 0
        self.closures = 0
        self.results: list[Response | Exception] = []
        self.before_get: Callable[[], None] | None = None
        self.after_post: Callable[[], None] | None = None
        self.failure_get: Exception | None = None
        self.last_response: FakeResponse | None = None

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str | list[str]],
        timeout: float,
        allow_redirects: bool,
    ) -> Response:
        self.queries += 1
        assert timeout > 0
        assert allow_redirects is False
        if self.failure_get is not None:
            raise self.failure_get
        if self.before_get is not None:
            self.before_get()
        return FakeResponse(self.document)

    def post(
        self,
        url: str,
        *,
        json: Mapping[str, object],
        timeout: float,
        allow_redirects: bool,
    ) -> Response:
        assert url.endswith("document_synthetic:batchUpdate")
        assert timeout > 0
        assert allow_redirects is False
        self.sends.append(dict(deepcopy(json)))
        if self.results:
            result = self.results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        for request in require_list(json["requests"]):
            if "updateSheetProperties" in request:
                properties = require_object(
                    require_object(request["updateSheetProperties"])["properties"]
                )
                destination = self._by_id(properties["sheetId"])
                require_object(require_object(destination["properties"])["gridProperties"]).update(
                    require_object(properties["gridProperties"])
                )
                continue
            update = require_object(request["updateCells"])
            start = require_object(update["start"])
            destination = self._by_id(start["sheetId"])
            for offset, row in enumerate(require_list(update["rows"]), 1):
                content = require_list(row["values"])[0]
                current = cell(
                    destination,
                    cast(int, start["rowIndex"]) + offset,
                    cast(int, start["columnIndex"]) + 1,
                )
                current.pop("userEnteredValue", None)
                current.pop("effectiveValue", None)
                if "userEnteredValue" in content:
                    current["userEnteredValue"] = deepcopy(content["userEnteredValue"])
                    current["effectiveValue"] = deepcopy(content["userEnteredValue"])
                if "userEnteredFormat" in content:
                    format = require_object(current.setdefault("userEnteredFormat", {}))
                    format.pop("backgroundColor", None)
                    format.update(deepcopy(require_object(content["userEnteredFormat"])))
                    current["effectiveFormat"] = deepcopy(format)
        if self.after_post is not None:
            self.after_post()
        self.last_response = FakeResponse({"spreadsheetId": "document_synthetic"})
        return self.last_response

    def _by_id(self, identifier: object) -> Object:
        return next(
            destination
            for destination in require_list(self.document["sheets"])
            if require_object(destination["properties"])["sheetId"] == identifier
        )

    def close(self) -> None:
        self.closures += 1


@pytest.fixture(autouse=True)
def isolate_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(environ):
        if name.upper().startswith("CA_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def document() -> Object:
    result: Object = {"spreadsheetId": "document_synthetic", "sheets": []}
    for catalog_index, name in enumerate(("Inviu", "IOL", "Bull", "Balanz", "PPI")):
        destination: Object = {
            "properties": {
                "sheetId": catalog_index * 10,
                "title": name,
                "gridProperties": {"rowCount": 100, "columnCount": 4},
            },
            "data": [{"rowData": []}],
        }
        cell(destination, 1, 1)["userEnteredValue"] = {"stringValue": "Client"}
        cell(destination, 2, 4 if name == "Balanz" else 3)["userEnteredValue"] = {
            "stringValue": "Week"
        }
        cell(destination, 3, 1)["userEnteredValue"] = {"stringValue": "Axa Sample"}
        cell(destination, 5, 1)["userEnteredValue"] = {"stringValue": "Notuka Example"}
        cell(destination, 3, 2)["userEnteredValue"] = {"formulaValue": "=1+2"}
        require_list(result["sheets"]).append(destination)
    destination = sheet(result)
    cell(destination, 5, 3)["userEnteredValue"] = {"stringValue": " X "}
    cell(destination, 3, 3)["userEnteredFormat"] = {"textFormat": {"bold": True}}
    cell(destination, 4, 3)["note"] = "Gap that no must written"
    return result


def reader(session: FakeTransport) -> SheetsClientReader:
    return SheetsClientReader(
        "document_synthetic",
        session=session,
        attempts=3,
        wait_minimum_seconds=0.001,
        wait_maximum_seconds=0.002,
    )


def writer(session: FakeTransport, tmp_path: Path) -> SheetsClientWriter:
    return SheetsClientWriter(
        "document_synthetic",
        session=session,
        backup_directory=tmp_path / "backups",
        clock=FixedClock(),
        attempts=3,
        wait_minimum_seconds=0.001,
        wait_maximum_seconds=0.002,
    )


def apply(destination: SheetsClientWriter, revision: str) -> None:
    destination.apply_marks("IOL", 3, "Week", frozenset({3}), expected_revision=revision)


def test_constructor_no_does_io_and_satisfies_contract(document: Object, tmp_path: Path) -> None:
    session = FakeTransport(document)
    destination: ClientWriter = writer(session, tmp_path)
    assert destination is not None
    assert session.queries == 0
    assert not list(tmp_path.iterdir())


def test_replaces_only_clients_and_preserves_gaps_formulas_and_styles(
    document: Object,
    tmp_path: Path,
) -> None:
    session = FakeTransport(document)
    read = reader(session)
    revision = read.revision()
    original = deepcopy(document)
    apply(writer(session, tmp_path), revision)
    assert session.last_response is not None
    assert session.last_response.closures == 1
    assert len(session.sends) == 1
    assert read.revision() != revision
    assert read.read_marks("IOL", 3) == frozenset({3})
    for name in ("Inviu", "Bull", "Balanz", "PPI"):
        assert sheet(document, name) == sheet(original, name)
    destination = sheet(document)
    assert cell(destination, 4, 3) == cell(sheet(original), 4, 3)
    assert cell(destination, 3, 2) == cell(sheet(original), 3, 2)
    assert require_object(cell(destination, 3, 3)["userEnteredFormat"])["textFormat"] == {
        "bold": True
    }
    assert "userEnteredValue" not in cell(destination, 5, 3)
    assert not (tmp_path / "backups").exists()  # The service decides when a backup is necessary.


@pytest.mark.parametrize(("name", "column"), [("IOL", 4), ("Balanz", 5)])
def test_creates_week_literal_and_expands_only_if_missing_space(
    document: Object,
    tmp_path: Path,
    name: str,
    column: int,
) -> None:
    session = FakeTransport(document)
    revision = reader(session).revision()
    writer(session, tmp_path).apply_marks(
        name, column, " New ", frozenset({3}), expected_revision=revision
    )
    read = reader(session)
    assert read.last_week(name) == (column, "New")
    assert read.read_marks(name, column) == frozenset({3})
    assert require_object(require_object(sheet(document, name)["properties"])["gridProperties"])[
        "columnCount"
    ] == max(4, column)
    assert all(
        "appendDimension" not in request for request in require_list(session.sends[0]["requests"])
    )


def test_backup_exclusive_contains_snapshot_and_no_writes_remote(
    document: Object,
    tmp_path: Path,
) -> None:
    session = FakeTransport(document)
    destination = writer(session, tmp_path)
    first = destination.create_backup()
    second = destination.create_backup()
    assert first != second
    content = require_object(json.loads(first.read_text(encoding="utf-8")))
    assert content["version"] == 1
    assert content["document"] == document
    assert content["revision"] == reader(session).revision()
    assert not session.sends


def test_change_during_backup_removes_copy(document: Object, tmp_path: Path) -> None:
    session = FakeTransport(document)

    def change() -> None:
        if session.queries == 2:
            cell(sheet(document), 3, 1)["userEnteredValue"] = {"stringValue": "Other Person"}

    session.before_get = change
    with pytest.raises(ConcurrencyError):
        writer(session, tmp_path).create_backup()
    assert not list((tmp_path / "backups").iterdir())
    assert not session.sends


def test_error_of_disk_no_writes_remote(
    document: Object,
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    session = FakeTransport(document)
    mocker.patch.object(module, "fsync", side_effect=OSError("fake"))
    with pytest.raises(PersistenceError):
        writer(session, tmp_path).create_backup()
    assert not list((tmp_path / "backups").iterdir())
    assert not session.sends


def test_revision_stale_no_sends_batch(document: Object, tmp_path: Path) -> None:
    session = FakeTransport(document)
    with pytest.raises(ConcurrencyError):
        apply(writer(session, tmp_path), "stale")
    assert not session.sends


@pytest.mark.parametrize("state", [408, 429, 500, 502, 503, 504])
def test_retry_http_checks_revision_before_of_resend(
    document: Object,
    tmp_path: Path,
    state: int,
) -> None:
    session = FakeTransport(document)
    revision = reader(session).revision()
    session.results = [FakeResponse({}, state)]
    apply(writer(session, tmp_path), revision)
    assert len(session.sends) == 2
    assert session.queries == 5
    assert session.sends[0] == session.sends[1]


@pytest.mark.parametrize("cause", [Timeout("fake"), ConnectionError("fake")])
def test_retries_of_connection_are_bounded(
    document: Object,
    tmp_path: Path,
    cause: Exception,
) -> None:
    session = FakeTransport(document)
    revision = reader(session).revision()
    session.results = [cause, cause, cause]
    with pytest.raises(TransientError):
        apply(writer(session, tmp_path), revision)
    assert len(session.sends) == 3
    assert session.queries == 7


def test_timeout_after_apply_stops_resend_by_revision(
    document: Object,
    tmp_path: Path,
) -> None:
    session = FakeTransport(document)
    revision = reader(session).revision()

    def lose_confirmation() -> None:
        raise Timeout("the synthetic response was lost")

    session.after_post = lose_confirmation
    with pytest.raises(ConcurrencyError):
        apply(writer(session, tmp_path), revision)
    assert len(session.sends) == 1
    assert reader(session).read_marks("IOL", 3) == frozenset({3})


@pytest.mark.parametrize(
    ("state", "category"),
    [
        (400, StructureError),
        (401, AuthenticationError),
        (403, AuthorizationError),
        (404, AuthorizationError),
        (501, ExternalServiceError),
        (505, ExternalServiceError),
    ],
)
def test_error_permanent_no_retries_and_closes_response(
    document: Object,
    tmp_path: Path,
    state: int,
    category: type[Exception],
) -> None:
    session = FakeTransport(document)
    response = FakeResponse({"error": "secret fake"}, state)
    session.results = [response]
    with pytest.raises(category) as error:
        apply(writer(session, tmp_path), reader(session).revision())
    assert len(session.sends) == 1
    assert response.closures == 1
    assert "secret fake" not in str(error.value)


def test_tls_no_retries(document: Object, tmp_path: Path) -> None:
    session = FakeTransport(document)
    session.results = [SSLError("fake")]
    with pytest.raises(ExternalServiceError):
        apply(writer(session, tmp_path), reader(session).revision())
    assert len(session.sends) == 1


def test_failure_of_read_no_multiplies_retries(document: Object, tmp_path: Path) -> None:
    session = FakeTransport(document)
    revision = reader(session).revision()
    session.failure_get = Timeout("fake")
    with pytest.raises(OperationTimeoutError):
        apply(writer(session, tmp_path), revision)
    assert session.queries == 4
    assert not session.sends


@pytest.mark.parametrize("content", [ValueError("fake"), {}, [], {"spreadsheetId": "other"}])
def test_confirmation_invalid_no_repeats_post(
    document: Object,
    tmp_path: Path,
    content: object,
) -> None:
    session = FakeTransport(document)
    session.results = [FakeResponse(content)]
    with pytest.raises(StructureError):
        apply(writer(session, tmp_path), reader(session).revision())
    assert len(session.sends) == 1


@pytest.mark.parametrize(
    "field", ["merges", "protectedRanges", "bandedRanges", "conditionalFormats"]
)
def test_rejects_restrictions_overlapping_but_accepts_other_column(
    document: Object,
    tmp_path: Path,
    field: str,
) -> None:
    destination = sheet(document)
    period = {"sheetId": 10, "startColumnIndex": 2, "endColumnIndex": 3}
    destination[field] = [
        period
        if field == "merges"
        else {"ranges": [period]}
        if field == "conditionalFormats"
        else {"range": period}
    ]
    session = FakeTransport(document)
    with pytest.raises(StructureError):
        apply(writer(session, tmp_path), reader(session).revision())
    assert not session.sends
    period["startColumnIndex"] = 1
    period["endColumnIndex"] = 2
    apply(writer(session, tmp_path), reader(session).revision())
    assert len(session.sends) == 1


@pytest.mark.parametrize(
    "value",
    [
        {"formulaValue": "=1"},
        {"numberValue": 3},
        {"stringValue": "other"},
        {"errorValue": {"type": "REF"}},
    ],
)
@pytest.mark.parametrize("field", ["userEnteredValue", "effectiveValue"])
def test_rejects_data_unrelated_in_week(
    document: Object,
    tmp_path: Path,
    value: Object,
    field: str,
) -> None:
    cell(sheet(document), 3, 3)[field] = value
    session = FakeTransport(document)
    with pytest.raises(StructureError):
        apply(writer(session, tmp_path), reader(session).revision())
    assert not session.sends


@pytest.mark.parametrize("case", ["tables", "pivotTable", "dataSourceFormula", "dataValidation"])
def test_rejects_tables_pivots_and_validations(
    document: Object,
    tmp_path: Path,
    case: str,
) -> None:
    if case == "tables":
        sheet(document)[case] = [{"tableId": "fake"}]
    else:
        cell(sheet(document), 3, 3)[case] = {"fake": True}
    session = FakeTransport(document)
    with pytest.raises(StructureError):
        apply(writer(session, tmp_path), reader(session).revision())
    assert not session.sends


@pytest.mark.parametrize(
    ("name", "column", "header", "rows"),
    [
        ("Other", 3, "Week", frozenset({3})),
        ("IOL", 2, "Week", frozenset({3})),
        ("IOL", 5, "New", frozenset({3})),
        ("IOL", 3, "=SUM(A1)", frozenset({3})),
        ("IOL", 3, "", frozenset({3})),
        ("IOL", 3, "Week", frozenset({4})),
        ("IOL", 3, "Week", frozenset({True})),
    ],
)
def test_request_incompatible_no_writes(
    document: Object,
    tmp_path: Path,
    name: str,
    column: int,
    header: str,
    rows: frozenset[int],
) -> None:
    session = FakeTransport(document)
    with pytest.raises(StructureError):
        writer(session, tmp_path).apply_marks(
            name, column, header, rows, expected_revision=reader(session).revision()
        )
    assert not session.sends


def test_closure_respects_ownership_e_invalid(document: Object, tmp_path: Path) -> None:
    session = FakeTransport(document)
    with writer(session, tmp_path) as destination:
        pass
    assert session.closures == 0
    destination.close()
    with pytest.raises(ConfigurationError):
        destination.create_backup()
    with pytest.raises(ConfigurationError):
        apply(destination, "revision")


def test_factory_rejects_only_read_without_create_session(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    make = mocker.patch.object(module, "AuthorizedSession")
    configuration = Config(
        base_directory=tmp_path, spreadsheet_id="document_synthetic", _env_file=None
    )
    with pytest.raises(ConfigurationError):
        create_writer_sheets(
            configuration,
            cast(Callable[[], AnonymousCredentials], AnonymousCredentials)(),
            clock=FixedClock(),
        )
    make.assert_not_called()


def test_factory_configures_and_closes_both_sessions(tmp_path: Path, mocker: MockerFixture) -> None:
    scope = "https://www.googleapis.com/auth/spreadsheets"
    configuration = Config(
        base_directory=tmp_path,
        spreadsheet_id="document_synthetic",
        google_scopes=(scope,),
        _env_file=None,
    )
    make_credential = cast(Callable[..., Credentials], Credentials)
    credential = make_credential(token="fake", scopes=[scope])
    authorized = mocker.Mock(spec=Session)
    refresh = mocker.Mock(spec=Session)
    make = mocker.patch.object(module, "AuthorizedSession", return_value=authorized)
    mocker.patch.object(module, "Session", return_value=refresh)
    with create_writer_sheets(configuration, credential, clock=FixedClock()) as destination:
        assert destination is not None
    assert make.call_args.kwargs["max_refresh_attempts"] == 0
    assert make.call_args.kwargs["refresh_timeout"] == configuration.network_timeout_seconds
    authorized.close.assert_called_once()
    refresh.close.assert_called_once()


@pytest.mark.parametrize("fail", [False, True])
def test_service_registration_dry_runs_backs_up_and_detects_result_uncertain(
    document: Object,
    tmp_path: Path,
    fail: bool,
) -> None:
    session = FakeTransport(document)
    read = reader(session)
    revision = read.revision()
    catalog = build_catalog(
        source_id="sheets_synthetic",
        revision=revision,
        generated_at=FixedClock().now(),
        sheets=read.list_sheets(),
        clients_by_sheet={info.name: read.read_clients(info.name) for info in read.list_sheets()},
    )
    week = WeeklyContacts(
        period=WeeklyPeriod(start_date=date(2026, 11, 2), end_date=date(2026, 11, 6)),
        contacts=(),
    )
    service = RegistrationService(
        reader_clients=read,
        writer_clients=writer(session, tmp_path),
        source_id="sheets_synthetic",
    )
    plan = service.plan(catalog=catalog, week=week, resolution=WeeklyResolution(), header="New")
    assert service.run(plan).dry_run
    assert not session.sends
    assert not (tmp_path / "backups").exists()
    original = deepcopy(document)
    backup: Path | str | None
    if fail:

        def lose_confirmation() -> None:
            raise Timeout("fake")

        session.after_post = lose_confirmation
        with pytest.raises(RegistrationInterruptedError) as error:
            service.run(plan, dry_run=False)
        assert error.value.confirmed_sheets == ()
        assert error.value.current_sheet == "Inviu"
        assert len(session.sends) == 1
        backup = error.value.backup
    else:
        result = service.run(plan, dry_run=False)
        assert result.applied_sheets == ("Inviu", "IOL", "Bull", "Balanz", "PPI")
        assert all(read.last_week(info.name)[1] == "New" for info in read.list_sheets())
        backup = result.backup
    assert isinstance(backup, Path)
    assert require_object(json.loads(backup.read_text(encoding="utf-8")))["document"] == original


def test_change_between_preparation_and_send_no_writes(document: Object, tmp_path: Path) -> None:
    session = FakeTransport(document)
    revision = reader(session).revision()

    def change() -> None:
        if session.queries == 3:
            cell(sheet(document), 5, 1)["userEnteredValue"] = {"stringValue": "Other Person"}

    session.before_get = change
    with pytest.raises(ConcurrencyError):
        apply(writer(session, tmp_path), revision)
    assert not session.sends


def test_repeat_week_new_no_duplicates_columns(document: Object, tmp_path: Path) -> None:
    session = FakeTransport(document)
    destination = writer(session, tmp_path)
    for _ in range(2):
        destination.apply_marks(
            "Balanz", 5, "New", frozenset({3}), expected_revision=reader(session).revision()
        )
    read = reader(session)
    assert read.read_headers("Balanz") == ((4, "Week"), (5, "New"))
    assert read.read_marks("Balanz", 5) == frozenset({3})
    assert (
        require_object(require_object(sheet(document, "Balanz")["properties"])["gridProperties"])[
            "columnCount"
        ]
        == 5
    )


def test_sheet_empty_without_weeks_creates_first_in_d_for_balanz(
    document: Object,
    tmp_path: Path,
) -> None:
    destination = sheet(document, "Balanz")
    destination["data"] = [
        {"rowData": [{"values": [{"userEnteredValue": {"stringValue": "Client"}}]}]}
    ]
    session = FakeTransport(document)
    writer(session, tmp_path).apply_marks(
        "Balanz", 4, "First", frozenset(), expected_revision=reader(session).revision()
    )
    assert reader(session).last_week("Balanz") == (4, "First")
    assert len(require_list(session.sends[0]["requests"])) == 1


def test_snapshot_public_no_shares_state_mutable(document: Object) -> None:
    session = FakeTransport(document)
    read = reader(session)
    revision, snapshot = read.read_snapshot()
    require_object(sheet(snapshot)["properties"])["title"] = "Modified"
    cell(sheet(snapshot, "Inviu"), 3, 1)["userEnteredValue"] = {"stringValue": "Tampered"}
    assert read.list_sheets()[1].name == "IOL"
    assert read.read_clients("Inviu")[0].raw_name == "Axa Sample"
    assert read.revision() == revision


@pytest.mark.parametrize(
    "cause",
    [
        cast(Callable[[str], GoogleAuthError], GoogleAuthError)("fake"),
        cast(Callable[[str], RefreshError], RefreshError)("fake"),
        RequestException("fake"),
    ],
)
def test_errors_of_authorization_or_request_no_retry(
    document: Object,
    tmp_path: Path,
    cause: Exception,
) -> None:
    session = FakeTransport(document)
    session.results = [cause]
    with pytest.raises((AuthenticationError, ExternalServiceError)):
        apply(writer(session, tmp_path), reader(session).revision())
    assert len(session.sends) == 1


@pytest.mark.parametrize("case", ["without_id", "without_permission", "fails_constructor"])
def test_factory_rejects_configuration_or_credentials_and_releases_resources(
    tmp_path: Path,
    mocker: MockerFixture,
    case: str,
) -> None:
    scope = "https://www.googleapis.com/auth/spreadsheets"
    configuration = Config(
        base_directory=tmp_path,
        spreadsheet_id=None if case == "without_id" else "document_synthetic",
        google_scopes=(scope,),
        _env_file=None,
    )
    make_credential = cast(Callable[..., Credentials], Credentials)
    credential = make_credential(
        token="fake", scopes=[] if case == "without_permission" else [scope]
    )
    refresh = mocker.Mock(spec=Session)
    mocker.patch.object(module, "Session", return_value=refresh)
    make = mocker.patch.object(module, "AuthorizedSession", side_effect=RuntimeError("fake"))
    category = {
        "without_id": ConfigurationError,
        "without_permission": AuthorizationError,
        "fails_constructor": RuntimeError,
    }[case]
    with pytest.raises(category):
        create_writer_sheets(configuration, credential, clock=FixedClock())
    if case == "fails_constructor":
        refresh.close.assert_called_once()
    else:
        make.assert_not_called()


def test_closure_failed_preserves_exception_of_block(
    document: Object,
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    session = FakeTransport(document)
    destination = SheetsClientWriter(
        "document_synthetic",
        session=session,
        backup_directory=tmp_path,
        clock=FixedClock(),
        close_session=True,
    )
    mocker.patch.object(session, "close", side_effect=OSError("fake"))
    with pytest.raises(RuntimeError, match="original"), destination:
        raise RuntimeError("original")
    destination.close()


@pytest.mark.parametrize(
    "period",
    [
        {"sheetId": 99},
        {"sheetId": 10, "startColumnIndex": -1},
        {"sheetId": 10, "startColumnIndex": 3, "endColumnIndex": 2},
    ],
)
def test_rejects_limits_incompatible_of_restrictions(
    document: Object,
    tmp_path: Path,
    period: Object,
) -> None:
    sheet(document)["protectedRanges"] = [{"range": period}]
    session = FakeTransport(document)
    with pytest.raises(StructureError):
        apply(writer(session, tmp_path), reader(session).revision())
    assert not session.sends


def test_batch_oversized_no_fragments_nor_sends(
    document: Object,
    tmp_path: Path,
) -> None:
    destination = sheet(document)
    require_object(require_object(destination["properties"])["gridProperties"])["rowCount"] = 22000
    for catalog_index in range(6, 22000):
        cell(destination, catalog_index, 1)["userEnteredValue"] = {
            "stringValue": "Synthetic Client"
        }
    session = FakeTransport(document)
    with pytest.raises(StructureError, match="batch exceeds"):
        apply(writer(session, tmp_path), reader(session).revision())
    assert not session.sends
