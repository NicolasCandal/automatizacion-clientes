"""Sheets snapshots, colors, and HTTP errors without live connections or credentials."""

from collections.abc import Callable, Mapping
from copy import deepcopy
from os import environ
from pathlib import Path
from typing import cast

import pytest
from google.auth.credentials import AnonymousCredentials, Credentials
from google.auth.exceptions import GoogleAuthError, RefreshError, TransportError
from pytest_mock import MockerFixture
from requests import Response, Session
from requests.exceptions import (
    ChunkedEncodingError,
    ConnectionError,
    RequestException,
    SSLError,
    Timeout,
)

from client_automation.config import Config
from client_automation.core.contracts import RegistrationClientReader
from client_automation.core.errors import (
    AuthenticationError,
    AuthorizationError,
    ConfigurationError,
    ExternalServiceError,
    OperationTimeoutError,
    StructureError,
    TransientError,
)
from client_automation.infra import sheets_client_reader as module
from client_automation.infra.sheets_client_reader import (
    SheetsClientReader,
    create_reader_sheets,
)

type Object = dict[str, object]
GREEN = {"red": 118 / 255, "green": 146 / 255, "blue": 60 / 255}


def error_google[ErrorType: GoogleAuthError](kind: type[ErrorType]) -> ErrorType:
    # The external constructor is untyped; test only its public argument.
    make = cast(Callable[[str], ErrorType], kind)
    return make("secret")


class FakeResponse(Response):
    def __init__(self, document: object, state: int = 200) -> None:
        super().__init__()
        self.document = document
        self.status_code = state
        self.closures = 0

    def json(self, **kwargs: object) -> object:
        if isinstance(self.document, Exception):
            raise self.document
        return self.document

    def close(self) -> None:
        self.closures += 1


class FakeSession:
    def __init__(self, *results: object) -> None:
        self.results = list(results)
        self.requests: list[tuple[str, Mapping[str, str | list[str]], float, bool]] = []
        self.closures = 0

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str | list[str]],
        timeout: float,
        allow_redirects: bool,
    ) -> Response:
        self.requests.append((url, params, timeout, allow_redirects))
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return result if isinstance(result, FakeResponse) else FakeResponse(deepcopy(result))

    def close(self) -> None:
        self.closures += 1


def text(value: str) -> Object:
    return {
        "userEnteredValue": {"stringValue": value},
        "effectiveValue": {"stringValue": value},
        "formattedValue": value,
    }


def sheet(document: Object, name: str = "IOL") -> Object:
    return next(
        element
        for element in cast(list[Object], document["sheets"])
        if cast(Object, element["properties"])["title"] == name
    )


def cell(destination: Object, row: int, column: int, content: Object) -> None:
    cast(list[Object], destination["data"]).append(
        {
            "startRow": row - 1,
            "startColumn": column - 1,
            "rowData": [{"values": [content]}],
        }
    )


def find_cell(document: Object, row: int, column: int, name: str = "IOL") -> Object:
    block = next(
        block
        for block in cast(list[Object], sheet(document, name)["data"])
        if block["startRow"] == row - 1 and block["startColumn"] == column - 1
    )
    rows = cast(list[Object], block["rowData"])
    return cast(list[Object], rows[0]["values"])[0]


@pytest.fixture
def document() -> Object:
    sheets: list[Object] = []
    for catalog_index, name in enumerate(("Inviu", "IOL", "Bull", "Balanz", "PPI")):
        destination: Object = {
            "properties": {
                "title": name,
                "sheetId": catalog_index * 10,
                "sheetType": "GRID",
                "gridProperties": {"rowCount": 5005, "columnCount": 800},
            },
            "data": [],
        }
        cell(destination, 1, 1, text("Client"))
        if name != "PPI":
            cell(destination, 2, 4 if name == "Balanz" else 3, text("02 on 06-11"))
            cell(destination, 3, 1, text("Axa Sample"))
        sheets.append(destination)
    result: Object = {"spreadsheetId": "document_synthetic", "sheets": sheets}
    destination = sheet(result)
    find_cell(result, 3, 1)["userEnteredValue"] = {"stringValue": "  Axa Sample  "}
    cell(destination, 2, 4, text(" 09 on 13-11 "))
    cell(destination, 5, 1, text("Notuka Example"))
    cell(destination, 6, 1, text("   "))
    cell(destination, 3005, 1, text("AXA SAMPLE AND/OR NOTUKA EXAMPLE"))
    cell(destination, 3, 4, {"effectiveFormat": {"backgroundColorStyle": {"rgbColor": GREEN}}})
    cell(
        destination,
        5,
        4,
        {"effectiveFormat": {"backgroundColor": {"red": 1, "green": 1, "blue": 1}}},
    )
    cell(destination, 3005, 4, text(" X "))
    cell(destination, 4500, 4, {"effectiveFormat": {"backgroundColorStyle": {"rgbColor": GREEN}}})
    cell(sheet(result, "Balanz"), 2, 3, text("Capital fixed"))
    return result


def reader(session: FakeSession) -> SheetsClientReader:
    return SheetsClientReader(
        "document_synthetic",
        session=session,
        wait_minimum_seconds=0.001,
        wait_maximum_seconds=0.002,
    )


def test_constructor_and_queries_reuse_snapshot_end_date_revision(document: Object) -> None:
    session = FakeSession(document)
    read: RegistrationClientReader = reader(session)
    assert not session.requests
    sheets = read.list_sheets()
    assert tuple(sheet.name for sheet in sheets) == ("Inviu", "IOL", "Bull", "Balanz", "PPI")
    assert sheets[1].sheet_id == 10
    assert sheets[1].last_client_row == 3005
    assert sheets[-1].last_client_row == 2
    assert read.last_week("PPI") == (1, "")
    assert read.read_clients("PPI") == ()
    assert read.read_headers("IOL") == ((3, "02 on 06-11"), (4, " 09 on 13-11 "))
    assert read.last_week("Balanz") == (4, "02 on 06-11")
    assert read.read_marks("IOL", 4) == frozenset({3, 3005})
    assert tuple(client.row for client in read.read_clients("IOL")) == (3, 5, 3005)
    assert read.read_clients("IOL")[0].raw_name == "  Axa Sample  "
    assert len(session.requests) == 1
    url, parameters, timeout, redirects = session.requests[0]
    assert url.endswith("/document_synthetic")
    assert parameters["ranges"] == ["'Inviu'", "'IOL'", "'Bull'", "'Balanz'", "'PPI'"]
    assert "effectiveFormat" not in str(
        parameters["fields"]
    )  # The data selector requests all fields in that subtree.
    assert "data" in str(parameters["fields"])
    assert timeout == 30.0 and not redirects
    read.revision()
    assert len(session.requests) == 2


def test_new_rows_and_columns_exceed_limits_of_reader_previous(document: Object) -> None:
    cell(sheet(document), 2, 750, text("Distant week"))
    cell(sheet(document), 5000, 1, text("Client later"))
    cell(sheet(document), 5000, 750, text("x"))
    read = reader(FakeSession(document))
    assert read.last_week("IOL") == (750, "Distant week")
    assert read.read_marks("IOL", 750) == frozenset({5000})
    assert read.list_sheets()[1].last_client_row == 5000


@pytest.mark.parametrize("change", ["name", "format", "rule", "theme"])
def test_revision_refreshes_and_detects_content_and_format(document: Object, change: str) -> None:
    session = FakeSession(document)
    read = reader(session)
    previous = read.revision()
    if change == "name":
        find_cell(document, 5, 1)["userEnteredValue"] = {"stringValue": "Other Person"}
        assert read.read_clients("IOL")[1].raw_name == "Notuka Example"
    elif change == "format":
        find_cell(document, 5, 4)["userEnteredFormat"] = {"textFormat": {"bold": True}}
    elif change == "rule":
        sheet(document)["conditionalFormats"] = [{"ranges": [{"sheetId": 10}]}]
    else:
        document["properties"] = {"spreadsheetTheme": {"primaryFontFamily": "Arial"}}
    assert read.revision() != previous
    if change == "name":
        assert read.read_clients("IOL")[1].raw_name == "Other Person"


def test_order_of_keys_json_no_modifies_revision(document: Object) -> None:
    read = reader(FakeSession(document, dict(reversed(tuple(document.items())))))
    assert read.revision() == read.revision()


def test_failure_of_refresh_invalid_the_snapshot_previous(document: Object) -> None:
    session = FakeSession(document, {"spreadsheetId": "document_synthetic", "sheets": []})
    read = reader(session)
    read.list_sheets()
    with pytest.raises(StructureError):
        read.revision()
    with pytest.raises(StructureError):
        read.read_clients("IOL")
    assert len(session.requests) == 3


@pytest.mark.parametrize(
    ("column", "validates"),
    [(True, False), (0, False), (-1, False), (2, False), (5, False), (3, True)],
)
def test_columns_weekly_valid(document: Object, column: int, validates: bool) -> None:
    read = reader(FakeSession(document))
    if validates:
        assert read.read_marks("IOL", column) == frozenset()
    else:
        with pytest.raises(StructureError):
            read.read_marks("IOL", column)
    with pytest.raises(StructureError):
        read.read_marks("PPI", 3)


def test_closure_idempotent_and_ownership_of_the_session(document: Object) -> None:
    session = FakeSession(document)
    read = reader(session)
    with read:
        read.list_sheets()
    read.close()
    assert session.closures == 0
    with pytest.raises(ConfigurationError):
        read.revision()
    with pytest.raises(ConfigurationError):
        read.__enter__()
    owned = SheetsClientReader("document_synthetic", session=session, close_session=True)
    with pytest.raises(RuntimeError), owned:
        raise RuntimeError("Failure inside the block.")
    owned.close()
    assert session.closures == 1


def test_sheet_unrelated_rejects_without_query_network(document: Object) -> None:
    session = FakeSession(document)
    with pytest.raises(StructureError):
        reader(session).read_clients("Unconfigured")
    assert not session.requests


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("attempts", 0),
        ("attempts", True),
        ("attempts", 11),
        ("timeout_seconds", 0),
        ("timeout_seconds", float("inf")),
        ("wait_minimum_seconds", -1),
        ("wait_maximum_seconds", float("nan")),
        ("wait_minimum_seconds", 40),
    ],
)
def test_configuration_invalid_no_performs_io(field: str, value: float) -> None:
    session = FakeSession({})
    with pytest.raises(ConfigurationError):
        if field == "attempts":
            assert isinstance(value, int)
            SheetsClientReader("document", session=session, attempts=value)
        elif field == "timeout_seconds":
            SheetsClientReader("document", session=session, timeout_seconds=value)
        elif field == "wait_minimum_seconds":
            SheetsClientReader("document", session=session, wait_minimum_seconds=value)
        else:
            SheetsClientReader("document", session=session, wait_maximum_seconds=value)
    assert not session.requests


def test_identifier_empty_and_encoding_of_url() -> None:
    with pytest.raises(ConfigurationError):
        SheetsClientReader(" ", session=FakeSession({}))
    session = FakeSession({"spreadsheetId": "one/two?token=secret", "sheets": []})
    with pytest.raises(StructureError):
        SheetsClientReader("one/two?token=secret", session=session).revision()
    assert session.requests[0][0].endswith("one%2Ftwo%3Ftoken%3Dsecret")


@pytest.mark.parametrize("state", [408, 429, 500, 502, 503, 504])
def test_http_transient_retries_and_closes_each_response(document: Object, state: int) -> None:
    first = FakeResponse({}, state)
    last = FakeResponse(document)
    session = FakeSession(first, last)
    assert reader(session).list_sheets()
    assert len(session.requests) == 2
    assert first.closures == last.closures == 1


@pytest.mark.parametrize(
    ("state", "kind"),
    [
        (400, StructureError),
        (401, AuthenticationError),
        (403, AuthorizationError),
        (404, AuthorizationError),
        (301, ExternalServiceError),
        (501, ExternalServiceError),
        (505, ExternalServiceError),
    ],
)
def test_http_permanent_no_retries(state: int, kind: type[Exception]) -> None:
    response = FakeResponse({"error": {"message": "secret"}}, state)
    session = FakeSession(response)
    with pytest.raises(kind) as error:
        reader(session).revision()
    assert len(session.requests) == response.closures == 1
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("reason", ["rateLimitExceeded", "userRateLimitExceeded"])
def test_403_of_quota_temporary_retries(document: Object, reason: str) -> None:
    session = FakeSession(FakeResponse({"error": {"errors": [{"reason": reason}]}}, 403), document)
    reader(session).revision()
    assert len(session.requests) == 2


@pytest.mark.parametrize(
    "content",
    [
        None,
        [],
        {"error": None},
        {"error": {"errors": None}},
        {"error": {"errors": []}},
        {"error": {"errors": [{"reason": []}]}},
        {"error": {"errors": [{"reason": "rateLimitExceeded"}, {"reason": "accessDenied"}]}},
        ValueError("secret"),
    ],
)
def test_403_no_interpretable_or_mixed_no_retries(content: object) -> None:
    session = FakeSession(FakeResponse(content, 403))
    with pytest.raises(AuthorizationError):
        reader(session).revision()
    assert len(session.requests) == 1


@pytest.mark.parametrize(
    ("cause", "kind"),
    [
        (Timeout("secret"), OperationTimeoutError),
        (ConnectionError("secret"), TransientError),
        (ChunkedEncodingError("secret"), TransientError),
        (error_google(TransportError), TransientError),
    ],
)
def test_errors_of_network_retry_end_date_limit_and_preserve_cause(
    cause: Exception, kind: type[Exception]
) -> None:
    session = FakeSession(cause)
    with pytest.raises(kind) as error:
        reader(session).revision()
    assert len(session.requests) == 4
    assert error.value.__cause__ is cause
    assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    ("cause", "kind"),
    [
        (SSLError("secret"), ExternalServiceError),
        (RequestException("secret"), ExternalServiceError),
        (error_google(RefreshError), AuthenticationError),
        (error_google(GoogleAuthError), AuthenticationError),
    ],
)
def test_errors_permanent_no_retry(cause: Exception, kind: type[Exception]) -> None:
    session = FakeSession(cause)
    with pytest.raises(kind) as error:
        reader(session).revision()
    assert len(session.requests) == 1
    assert error.value.__cause__ is cause


def test_refresh_temporary_retries(document: Object) -> None:
    class TransientRefreshError(RefreshError):
        @property
        def retryable(self) -> bool:
            return True

    session = FakeSession(error_google(TransientRefreshError), document)
    reader(session).revision()
    assert len(session.requests) == 2


def test_json_invalid_closes_response_without_retry() -> None:
    response = FakeResponse(ValueError("secret"))
    session = FakeSession(response)
    with pytest.raises(StructureError):
        reader(session).revision()
    assert len(session.requests) == response.closures == 1


@pytest.mark.parametrize(
    "document_invalid",
    [
        None,
        [],
        {1: "key"},
        {"spreadsheetId": "other", "sheets": []},
        {"spreadsheetId": "document_synthetic", "sheets": {}},
        {"invalid": float("nan")},
        {"invalid": object()},
    ],
)
def test_response_invalid_no_produces_snapshot(document_invalid: object) -> None:
    with pytest.raises(StructureError):
        reader(FakeSession(document_invalid)).revision()


@pytest.mark.parametrize(
    "case",
    [
        "missing_sheet",
        "name_duplicate",
        "id_duplicate",
        "id_bool",
        "kind_sheet",
        "title",
        "dimension_bool",
        "row_outside",
        "column_outside",
        "block_outside",
        "cell_duplicate",
    ],
)
def test_structure_incompatible_rejects(document: Object, case: str) -> None:
    sheets = cast(list[Object], document["sheets"])
    destination = sheet(document)
    properties = cast(Object, destination["properties"])
    if case == "missing_sheet":
        sheets.pop()
    elif case == "name_duplicate":
        sheets.append(deepcopy(destination))
    elif case == "id_duplicate":
        properties["sheetId"] = 0
    elif case == "id_bool":
        properties["sheetId"] = True
    elif case == "kind_sheet":
        properties["sheetType"] = "DATA_SOURCE"
    elif case == "title":
        find_cell(document, 1, 1)["userEnteredValue"] = {"stringValue": "Incorrect"}
    elif case == "dimension_bool":
        cast(Object, properties["gridProperties"])["rowCount"] = True
    elif case == "row_outside":
        cast(list[Object], destination["data"]).append({"startRow": 5004, "rowData": [{}, {}]})
    elif case == "column_outside":
        cell(destination, 4, 801, text("outside"))
    elif case == "block_outside":
        cast(list[Object], destination["data"]).append({"startColumn": 800})
    else:
        cell(destination, 3, 1, text("Duplicate"))
    with pytest.raises(StructureError):
        reader(FakeSession(document)).revision()


@pytest.mark.parametrize(
    "value",
    [
        {"numberValue": 10},
        {"formulaValue": "=A1"},
        {"errorValue": {}},
        {"stringValue": 1},
        {"stringValue": "Name", "boolValue": True},
    ],
)
def test_names_must_be_text_literal(document: Object, value: Object) -> None:
    find_cell(document, 3, 1)["userEnteredValue"] = value
    with pytest.raises(StructureError):
        reader(FakeSession(document)).read_clients("IOL")


def test_names_derived_and_errors_effective_reject(document: Object) -> None:
    name = find_cell(document, 3, 1)
    name.pop("userEnteredValue")
    with pytest.raises(StructureError):
        reader(FakeSession(document)).revision()
    name["userEnteredValue"] = {"stringValue": "Axa"}
    name["effectiveValue"] = {"errorValue": {"type": "ERROR"}}
    with pytest.raises(StructureError):
        reader(FakeSession(document)).revision()


@pytest.mark.parametrize(
    ("value", "format", "expected"),
    [
        ({"numberValue": 123}, "Date visible", "Date visible"),
        ({"numberValue": 123}, None, "123"),
        ({"boolValue": True}, None, "True"),
    ],
)
def test_headers_literals_preserve_value_formatted(
    document: Object, value: Object, format: str | None, expected: str
) -> None:
    header = find_cell(document, 2, 4)
    header["userEnteredValue"] = value
    if format is None:
        header.pop("formattedValue")
    else:
        header["formattedValue"] = format
    assert reader(FakeSession(document)).last_week("IOL") == (4, expected)


def test_header_formula_or_format_invalid_rejects(document: Object) -> None:
    header = find_cell(document, 2, 4)
    header["userEnteredValue"] = {"formulaValue": "=C2"}
    with pytest.raises(StructureError):
        reader(FakeSession(document)).revision()
    header["userEnteredValue"] = {"numberValue": 123}
    header["formattedValue"] = 123
    with pytest.raises(StructureError):
        reader(FakeSession(document)).revision()


def test_color_theme_resolves_and_color_effective_overrides(document: Object) -> None:
    document["properties"] = {
        "spreadsheetTheme": {
            "themeColors": [{"colorType": "ACCENT1", "color": {"rgbColor": GREEN}}]
        }
    }
    mark = find_cell(document, 3, 4)
    mark["effectiveFormat"] = {
        "backgroundColorStyle": {"themeColor": "ACCENT1"},
        "backgroundColor": {},
    }
    assert reader(FakeSession(document)).read_marks("IOL", 4) == frozenset({3, 3005})
    mark["effectiveFormat"] = {"backgroundColorStyle": {"rgbColor": {}}, "backgroundColor": GREEN}
    mark["userEnteredFormat"] = {"backgroundColorStyle": {"rgbColor": GREEN}}
    assert reader(FakeSession(document)).read_marks("IOL", 4) == frozenset({3005})


def test_theme_no_resolved_prevents_only_read_marks_and_x_has_priority(document: Object) -> None:
    mark = find_cell(document, 3, 4)
    mark["effectiveFormat"] = {"backgroundColorStyle": {"themeColor": "ACCENT1"}}
    read = reader(FakeSession(document))
    assert read.read_clients("IOL")
    with pytest.raises(StructureError):
        read.read_marks("IOL", 4)
    mark["userEnteredValue"] = {"stringValue": "x"}
    assert reader(FakeSession(document)).read_marks("IOL", 4) == frozenset({3, 3005})


def test_format_default_and_absence_of_effective_no_use_format_entered(
    document: Object,
) -> None:
    mark = find_cell(document, 3, 4)
    mark.pop("effectiveFormat")
    mark["userEnteredFormat"] = {"backgroundColorStyle": {"rgbColor": GREEN}}
    assert reader(FakeSession(document)).read_marks("IOL", 4) == frozenset({3005})
    document["properties"] = {"defaultFormat": {"backgroundColorStyle": {"rgbColor": GREEN}}}
    assert reader(FakeSession(document)).read_marks("IOL", 4) == frozenset({3, 3005})


@pytest.mark.parametrize("component", [True, -0.1, 1.1, "green", 10**400])
def test_colors_invalid_no_interpret_as_absence_of_mark(
    document: Object, component: object
) -> None:
    find_cell(document, 3, 4)["effectiveFormat"] = {"backgroundColor": {"red": component}}
    with pytest.raises(StructureError):
        reader(FakeSession(document)).revision()


def test_factory_configures_timeout_retries_and_closes_both_sessions(
    tmp_path: Path,
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
    document: Object,
) -> None:
    for name in tuple(environ):
        if name.startswith("CA_"):
            monkeypatch.delenv(name)
    configuration = Config(
        base_directory=tmp_path, spreadsheet_id="document_synthetic", _env_file=None
    )
    credentials = cast(Credentials, mocker.Mock(spec=Credentials))
    build = mocker.patch.object(module, "AuthorizedSession", autospec=True)
    refresh = mocker.patch.object(module, "Session", autospec=True)
    read = create_reader_sheets(configuration, credentials)
    assert build.call_args.kwargs["max_refresh_attempts"] == 0
    assert build.call_args.kwargs["refresh_timeout"] == configuration.network_timeout_seconds
    build.return_value.get.assert_not_called()
    refresh.return_value.get.assert_not_called()
    build.return_value.get.return_value = FakeResponse(document)
    read.revision()
    assert (
        build.return_value.get.call_args.kwargs["timeout"] == configuration.network_timeout_seconds
    )
    read.close()
    read.close()
    build.return_value.close.assert_called_once()
    refresh.return_value.close.assert_called_once()


def test_factory_failed_releases_session_of_refresh(tmp_path: Path, mocker: MockerFixture) -> None:
    configuration = Config(base_directory=tmp_path, spreadsheet_id="document", _env_file=None)
    refresh = mocker.patch.object(module, "Session", autospec=True)
    mocker.patch.object(module, "AuthorizedSession", side_effect=RuntimeError("Simulated failure."))
    with pytest.raises(RuntimeError):
        create_reader_sheets(configuration, cast(Credentials, mocker.Mock(spec=Credentials)))
    refresh.return_value.close.assert_called_once()
    with pytest.raises(ConfigurationError):
        create_reader_sheets(
            configuration.model_copy(update={"spreadsheet_id": None}),
            cast(Credentials, mocker.Mock()),
        )


def test_factory_real_uses_credentials_fake_and_http_simulated(
    tmp_path: Path,
    document: Object,
    mocker: MockerFixture,
) -> None:
    make = cast(Callable[[], Credentials], AnonymousCredentials)
    credentials = make()
    configuration = Config(
        base_directory=tmp_path, spreadsheet_id="document_synthetic", _env_file=None
    )
    query = mocker.patch.object(Session, "request", return_value=FakeResponse(document))
    with create_reader_sheets(configuration, credentials) as read:
        assert read.read_clients("IOL")[0].raw_name == "  Axa Sample  "
    query.assert_called_once()
    assert query.call_args.kwargs["timeout"] == configuration.network_timeout_seconds
    assert query.call_args.kwargs["allow_redirects"] is False


def test_logs_no_expose_identifier_contacts_nor_error_technical(
    document: Object, caplog: pytest.LogCaptureFixture
) -> None:
    session = FakeSession(ConnectionError("detail secret"), document)
    with caplog.at_level("DEBUG"):
        reader(session).revision()
    assert "retrying" in caplog.text
    assert "Sheets snapshot validated" in caplog.text
    assert "document_synthetic" not in caplog.text
    assert "Axa Sample" not in caplog.text
    assert "detail secret" not in caplog.text


@pytest.mark.parametrize(
    "value",
    [
        {"numberValue": True},
        {"numberValue": "123"},
        {"numberValue": 10**400},
        {"boolValue": "true"},
    ],
)
def test_headers_numerical_and_booleans_must_have_kind_correct(
    document: Object, value: Object
) -> None:
    find_cell(document, 2, 4)["userEnteredValue"] = value
    with pytest.raises(StructureError):
        reader(FakeSession(document)).revision()


@pytest.mark.parametrize(
    "case", ["theme_duplicate", "theme_without_rgb", "kind_theme", "style_double"]
)
def test_themes_and_styles_ambiguous_reject(document: Object, case: str) -> None:
    pair: Object = {"colorType": "ACCENT1", "color": {"rgbColor": GREEN}}
    if case == "theme_duplicate":
        colors = [pair, pair]
    elif case == "theme_without_rgb":
        pair["color"] = {"themeColor": "ACCENT2"}
        colors = [pair]
    elif case == "kind_theme":
        pair["colorType"] = 12
        colors = [pair]
    else:
        colors = [pair]
        find_cell(document, 3, 4)["effectiveFormat"] = {
            "backgroundColorStyle": {"rgbColor": GREEN, "themeColor": "ACCENT1"}
        }
    document["properties"] = {"spreadsheetTheme": {"themeColors": colors}}
    with pytest.raises(StructureError):
        reader(FakeSession(document)).revision()


def test_failure_of_closure_no_hides_error_of_block(
    document: Object, mocker: MockerFixture
) -> None:
    session = FakeSession(document)
    mocker.patch.object(session, "close", side_effect=OSError("secret"))
    read = SheetsClientReader("document_synthetic", session=session, close_session=True)
    with pytest.raises(ValueError, match="original"), read:
        raise ValueError("original")
    other = SheetsClientReader("document_synthetic", session=session, close_session=True)
    with pytest.raises(ExternalServiceError), other:
        pass


def test_blocks_contiguous_use_offsets_and_omit_indices_zero(document: Object) -> None:
    destination = sheet(document, "PPI")
    destination["data"] = [
        {
            "rowData": [
                {"values": [text("Client")]},
                {"values": [{}, {}, text("Week")]},
                {"values": [text("Other Person"), {}, text("x")]},
            ]
        }
    ]
    read = reader(FakeSession(document))
    assert read.read_clients("PPI")[0].row == 3
    assert read.read_marks("PPI", 3) == frozenset({3})
