"""Synthetic credentials, mocked refresh HTTP, and OAuth without a real browser."""

import json
import logging
from collections.abc import Callable, Mapping
from copy import deepcopy
from os import environ
from pathlib import Path
from typing import cast

import pytest
from google.auth.credentials import Credentials
from google.auth.crypt import Signer
from google.oauth2.credentials import Credentials as UserCredentials
from google.oauth2.service_account import Credentials as ServiceCredentials
from google_auth_oauthlib.flow import InstalledAppFlow
from pydantic import ValidationError
from pytest_mock import MockerFixture
from requests import Request as FakeRequest
from requests import Response
from requests.adapters import HTTPAdapter
from requests.exceptions import ConnectionError, ConnectTimeout, SSLError, Timeout

from client_automation.config import Config, load_config
from client_automation.core.errors import (
    AuthenticationError,
    AuthorizationError,
    ConcurrencyError,
    ConfigurationError,
    ExternalServiceError,
    OperationTimeoutError,
    PersistenceError,
    StructureError,
    TransientError,
)
from client_automation.infra import google_authentication as module
from client_automation.infra.google_authentication import GoogleCredentialsProvider
from client_automation.infra.json_storage import JsonValue, read_json, save_json
from client_automation.infra.sheets_client_reader import create_reader_sheets

SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
type Object = dict[str, JsonValue]


@pytest.fixture(autouse=True)
def isolate_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture
) -> None:
    for name in tuple(environ):
        if name.upper().startswith("CA_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    mocker.patch("requests.sessions.get_netrc_auth", return_value=None)


@pytest.fixture
def configuration(tmp_path: Path) -> Config:
    return Config(
        base_directory=tmp_path,
        _env_file=None,
        max_attempts=3,
        retry_min_wait_seconds=0.001,
        retry_max_wait_seconds=0.002,
    )


@pytest.fixture
def token() -> Object:
    return {
        "token": "access_fake",
        "refresh_token": "refresh_fake",
        "client_id": "client_fake",
        "client_secret": "secret_fake",
        "token_uri": module.URI_TOKEN,
        "scopes": [SCOPE],
        "expiry": "2099-01-01T00:00:00Z",
    }


def credentials(document: Object) -> Credentials:
    make = cast(
        Callable[[Mapping[str, object]], Credentials], UserCredentials.from_authorized_user_info
    )
    return make(document)


def response(state: int = 200, **content: object) -> Response:
    result = Response()
    result.status_code = state
    result.encoding = "utf-8"
    result._content = json.dumps(
        content
        or {
            "access_token": "access_new_fake",
            "expires_in": 3600,
            "token_type": "Bearer",
            "scope": SCOPE,
        }
    ).encode("utf-8")
    _ = result.content
    return result


def write_client(configuration: Config) -> None:
    save_json(
        configuration.credentials_path,
        {
            "installed": {
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": module.URI_TOKEN,
                "client_id": "client_fake",
                "client_secret": "secret_fake",
                "redirect_uris": ["http://localhost"],
            }
        },
    )


def test_constructor_no_opens_files_nor_does_network(
    configuration: Config, mocker: MockerFixture
) -> None:
    read = mocker.patch.object(module, "read_json")
    send = mocker.patch.object(HTTPAdapter, "send")
    GoogleCredentialsProvider(configuration)
    read.assert_not_called()
    send.assert_not_called()
    assert not configuration.token_path.parent.exists()


def test_token_valid_no_needs_client_secret_nor_refresh(
    configuration: Config, token: Object, mocker: MockerFixture
) -> None:
    save_json(configuration.token_path, token)
    original = configuration.token_path.read_bytes()
    send = mocker.patch.object(HTTPAdapter, "send")
    result = GoogleCredentialsProvider(configuration).load(persist=True)
    assert result.token == "access_fake"
    assert result.valid
    send.assert_not_called()
    assert configuration.token_path.read_bytes() == original
    assert not configuration.credentials_path.exists()


def test_token_missing_no_opens_browser_nor_creates_directories(
    configuration: Config, mocker: MockerFixture
) -> None:
    flow = mocker.patch.object(InstalledAppFlow, "from_client_config")
    with pytest.raises(AuthenticationError):
        GoogleCredentialsProvider(configuration).load()
    flow.assert_not_called()
    assert not configuration.token_path.parent.exists()


@pytest.mark.parametrize("persist", [False, True])
def test_refresh_saves_only_if_requests(
    configuration: Config, token: Object, mocker: MockerFixture, persist: bool
) -> None:
    token["expiry"] = "2000-01-01T00:00:00Z"
    save_json(configuration.token_path, token)
    original = configuration.token_path.read_bytes()
    send = mocker.patch.object(HTTPAdapter, "send", return_value=response())
    result = GoogleCredentialsProvider(configuration).load(persist=persist)
    assert result.valid
    assert result.token == "access_new_fake"
    send.assert_called_once()
    assert send.call_args.kwargs["timeout"] == configuration.network_timeout_seconds
    assert (configuration.token_path.read_bytes() != original) is persist
    if persist:
        assert cast(Object, read_json(configuration.token_path))["token"] == "access_new_fake"


def test_scopes_saved_as_string_no_cause_conflict_false(
    configuration: Config, token: Object, mocker: MockerFixture
) -> None:
    token.update({"scopes": SCOPE, "expiry": "2000-01-01T00:00:00Z"})
    save_json(configuration.token_path, token)
    mocker.patch.object(HTTPAdapter, "send", return_value=response())
    assert GoogleCredentialsProvider(configuration).load(persist=True).valid


def test_refresh_false_rejects_token_expired_without_network(
    configuration: Config, token: Object, mocker: MockerFixture
) -> None:
    token["expiry"] = "2000-01-01T00:00:00Z"
    save_json(configuration.token_path, token)
    send = mocker.patch.object(HTTPAdapter, "send")
    with pytest.raises(AuthenticationError):
        GoogleCredentialsProvider(configuration).load(refresh=False)
    send.assert_not_called()


@pytest.mark.parametrize("state", [400, 401, 403])
def test_token_revoked_or_without_permissions_no_deletes_nor_retries(
    configuration: Config, token: Object, mocker: MockerFixture, state: int
) -> None:
    token["expiry"] = "2000-01-01T00:00:00Z"
    save_json(configuration.token_path, token)
    original = configuration.token_path.read_bytes()
    send = mocker.patch.object(
        HTTPAdapter, "send", return_value=response(state, error="invalid_grant")
    )
    with pytest.raises(AuthorizationError if state == 403 else AuthenticationError):
        GoogleCredentialsProvider(configuration).load(persist=True)
    send.assert_called_once()
    assert configuration.token_path.read_bytes() == original


@pytest.mark.parametrize("state", [408, 429, 500, 502, 503, 504])
def test_refresh_retries_failures_transient(
    configuration: Config, token: Object, mocker: MockerFixture, state: int
) -> None:
    token["expiry"] = "2000-01-01T00:00:00Z"
    save_json(configuration.token_path, token)
    send = mocker.patch.object(
        HTTPAdapter, "send", side_effect=[response(state, error="temporary"), response()]
    )
    assert GoogleCredentialsProvider(configuration).load().valid
    assert send.call_count == 2


def test_failure_persistent_respects_limit_without_retries_hidden(
    configuration: Config, token: Object, mocker: MockerFixture
) -> None:
    token["expiry"] = "2000-01-01T00:00:00Z"
    save_json(configuration.token_path, token)
    send = mocker.patch.object(HTTPAdapter, "send", side_effect=Timeout("secret"))
    with pytest.raises(OperationTimeoutError):
        GoogleCredentialsProvider(configuration).load()
    assert send.call_count == configuration.max_attempts


@pytest.mark.parametrize("cause", [SSLError("secret"), ConnectionError("secret")])
def test_tls_no_retries_and_connection_if(
    configuration: Config, token: Object, mocker: MockerFixture, cause: Exception
) -> None:
    token["expiry"] = "2000-01-01T00:00:00Z"
    save_json(configuration.token_path, token)
    send = mocker.patch.object(HTTPAdapter, "send", side_effect=cause)
    with pytest.raises(ExternalServiceError):
        GoogleCredentialsProvider(configuration).load()
    assert send.call_count == (1 if isinstance(cause, SSLError) else configuration.max_attempts)


def test_refresh_partial_of_permissions_no_overwrites_token(
    configuration: Config, token: Object, mocker: MockerFixture
) -> None:
    token["expiry"] = "2000-01-01T00:00:00Z"
    save_json(configuration.token_path, token)
    original = configuration.token_path.read_bytes()
    mocker.patch.object(
        HTTPAdapter,
        "send",
        return_value=response(access_token="new", expires_in=3600, scope="other_scope"),
    )
    with pytest.raises(AuthorizationError):
        GoogleCredentialsProvider(configuration).load(persist=True)
    assert configuration.token_path.read_bytes() == original


def test_change_of_token_during_refresh_no_overwrites(
    configuration: Config, token: Object, mocker: MockerFixture
) -> None:
    token["expiry"] = "2000-01-01T00:00:00Z"
    save_json(configuration.token_path, token)
    other = deepcopy(token)
    other["token"] = "external_fake"

    def respond(*arguments: object, **options: object) -> Response:
        save_json(configuration.token_path, other)
        return response()

    mocker.patch.object(HTTPAdapter, "send", side_effect=respond)
    with pytest.raises(ConcurrencyError):
        GoogleCredentialsProvider(configuration).load(persist=True)
    assert read_json(configuration.token_path) == other


def test_token_malformed_and_without_permissions_no_trigger_login(
    configuration: Config, token: Object, mocker: MockerFixture
) -> None:
    flow = mocker.patch.object(InstalledAppFlow, "from_client_config")
    token["scopes"] = ["https://www.googleapis.com/auth/drive"]
    save_json(configuration.token_path, token)
    with pytest.raises(AuthorizationError):
        GoogleCredentialsProvider(configuration).load()
    configuration.token_path.write_bytes(b"{invalido")
    with pytest.raises(StructureError):
        GoogleCredentialsProvider(configuration).load()
    flow.assert_not_called()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("token_uri", "https://example.invalid/token"),
        ("refresh_token", ""),
        ("client_id", None),
        ("type", "service_account"),
        ("scopes", None),
        ("expiry", "invalid date"),
    ],
)
def test_fields_invalid_no_send_to_network(
    configuration: Config, token: Object, mocker: MockerFixture, field: str, value: JsonValue
) -> None:
    token[field] = value
    save_json(configuration.token_path, token)
    send = mocker.patch.object(HTTPAdapter, "send")
    with pytest.raises((AuthenticationError, AuthorizationError)):
        GoogleCredentialsProvider(configuration).load()
    send.assert_not_called()


def test_permission_error_no_interprets_as_token_missing(
    configuration: Config, mocker: MockerFixture
) -> None:
    mocker.patch.object(Path, "read_text", side_effect=PermissionError("secret"))
    with pytest.raises(PersistenceError):
        GoogleCredentialsProvider(configuration).load()


def test_account_service_no_reads_nor_saves_token_of_user(
    configuration: Config, token: Object, mocker: MockerFixture
) -> None:
    configuration = configuration.model_copy(
        update={"google_authentication_type": "service_account"}
    )
    save_json(
        configuration.credentials_path,
        {
            "type": "service_account",
            "token_uri": module.URI_TOKEN,
            "client_email": "fake@example.invalid",
            "private_key": "key_fake",
        },
    )
    existing = credentials(token)
    make = mocker.patch.object(
        ServiceCredentials, "from_service_account_info", return_value=existing
    )
    assert GoogleCredentialsProvider(configuration).load(persist=True) is existing
    assert make.call_args.kwargs["scopes"] == configuration.google_scopes
    assert not configuration.token_path.exists()
    with pytest.raises(ConfigurationError):
        GoogleCredentialsProvider(configuration).authorize()


def test_account_service_real_with_signer_fake_refreshes_with_http_simulated(
    configuration: Config, mocker: MockerFixture
) -> None:
    class FakeSigner(Signer):
        @property
        def key_id(self) -> str:
            return "key_fake"

        def sign(self, message: bytes) -> bytes:
            return b"signature_fake"

    configuration = configuration.model_copy(
        update={"google_authentication_type": "service_account"}
    )
    save_json(
        configuration.credentials_path,
        {
            "type": "service_account",
            "token_uri": module.URI_TOKEN,
            "client_email": "fake@example.invalid",
            "private_key": "key_fake",
        },
    )
    mocker.patch("google.auth._service_account_info.from_dict", return_value=FakeSigner())
    send = mocker.patch.object(HTTPAdapter, "send", return_value=response())
    result = GoogleCredentialsProvider(configuration).load()
    assert result.valid
    send.assert_called_once()
    assert not configuration.token_path.exists()


@pytest.mark.parametrize("case", ["kind", "endpoint", "key"])
def test_account_service_invalid_no_sends_secrets(
    configuration: Config, mocker: MockerFixture, case: str
) -> None:
    configuration = configuration.model_copy(
        update={"google_authentication_type": "service_account"}
    )
    document: Object = {
        "type": "service_account",
        "token_uri": module.URI_TOKEN,
        "client_email": "fake@example.invalid",
        "private_key": "key_fake",
    }
    if case == "kind":
        document["type"] = "authorized_user"
    elif case == "endpoint":
        document["token_uri"] = "https://example.invalid/token"
    save_json(configuration.credentials_path, document)
    send = mocker.patch.object(HTTPAdapter, "send")
    with pytest.raises(AuthenticationError):
        GoogleCredentialsProvider(configuration).load()
    send.assert_not_called()


@pytest.mark.parametrize("persist", [False, True])
def test_oauth_initial_is_explicit_loopback_pkce_and_without_print(
    configuration: Config,
    token: Object,
    mocker: MockerFixture,
    capsys: pytest.CaptureFixture[str],
    persist: bool,
) -> None:
    write_client(configuration)
    result = credentials(token)
    flow = mocker.Mock()
    flow.run_local_server.return_value = result
    make = mocker.patch.object(InstalledAppFlow, "from_client_config", return_value=flow)
    assert GoogleCredentialsProvider(configuration).authorize(persist=persist) is result
    assert make.call_args.kwargs["autogenerate_code_verifier"] is True
    options = flow.run_local_server.call_args.kwargs
    assert options["host"] == options["bind_addr"] == "127.0.0.1"
    assert options["port"] == 0
    assert options["authorization_prompt_message"] == ""
    assert options["timeout_seconds"] == configuration.google_authorization_timeout_seconds
    assert options["open_browser"] is True
    flow.oauth2session.close.assert_called_once()
    assert configuration.token_path.exists() is persist
    assert capsys.readouterr().out == ""


def test_oauth_failed_preserves_token_and_closes_session(
    configuration: Config, token: Object, mocker: MockerFixture
) -> None:
    write_client(configuration)
    save_json(configuration.token_path, token)
    original = configuration.token_path.read_bytes()
    flow = mocker.Mock()
    flow.run_local_server.side_effect = RuntimeError("secret code")
    mocker.patch.object(InstalledAppFlow, "from_client_config", return_value=flow)
    with pytest.raises(AuthenticationError) as error:
        GoogleCredentialsProvider(configuration).authorize()
    assert "secret" not in str(error.value)
    flow.oauth2session.close.assert_called_once()
    assert configuration.token_path.read_bytes() == original


@pytest.mark.parametrize("case", ["auth_uri", "token_uri", "client_web"])
def test_oauth_rejects_endpoints_unrelated_and_clients_web(
    configuration: Config, mocker: MockerFixture, case: str
) -> None:
    write_client(configuration)
    document = cast(Object, read_json(configuration.credentials_path))
    installed = cast(Object, document["installed"])
    if case == "client_web":
        document = {"web": installed}
    else:
        installed[case] = "https://example.invalid/endpoint"
    save_json(configuration.credentials_path, document)
    make = mocker.patch.object(InstalledAppFlow, "from_client_config")
    with pytest.raises((AuthenticationError, StructureError)):
        GoogleCredentialsProvider(configuration).authorize()
    make.assert_not_called()


@pytest.mark.parametrize(
    ("cause", "attempts"),
    [(ConnectTimeout("secret"), 3), (Timeout("secret"), 1), (ConnectionError("secret"), 1)],
)
def test_code_of_one_only_usage_no_resends_if_result_is_uncertain(
    configuration: Config, mocker: MockerFixture, cause: Exception, attempts: int
) -> None:
    send = mocker.patch.object(HTTPAdapter, "send", side_effect=cause)
    request = FakeRequest("POST", module.URI_TOKEN, data="grant_type=authorization_code").prepare()
    with pytest.raises(TransientError):
        module._TokenAdapter(configuration, exchange=True).send(request)
    assert send.call_count == attempts


@pytest.mark.parametrize("state", [301, 400, 429, 500])
def test_exchange_no_retries_responses_http(
    configuration: Config, mocker: MockerFixture, state: int
) -> None:
    send = mocker.patch.object(HTTPAdapter, "send", return_value=response(state, error="secret"))
    request = FakeRequest("POST", module.URI_TOKEN).prepare()
    with pytest.raises((AuthenticationError, TransientError)):
        module._TokenAdapter(configuration, exchange=True).send(request)
    send.assert_called_once()


def test_adapter_rejects_other_methods_and_hosts(
    configuration: Config, mocker: MockerFixture
) -> None:
    send = mocker.patch.object(HTTPAdapter, "send")
    for method, url in (("GET", module.URI_TOKEN), ("POST", "https://example.invalid/token")):
        with pytest.raises(AuthenticationError):
            module._TokenAdapter(configuration, exchange=False).send(
                FakeRequest(method, url).prepare()
            )
    send.assert_not_called()


def test_logs_no_expose_tokens_keys_or_codes_and_restore_sdk(
    configuration: Config, token: Object, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
) -> None:
    write_client(configuration)
    logger = logging.getLogger("google_auth_oauthlib.flow")
    previous = logger.disabled
    flow = mocker.Mock()

    def authorize(**options: object) -> Credentials:
        logger.warning("synthetic_secret_code")
        return credentials(token)

    flow.run_local_server.side_effect = authorize
    mocker.patch.object(InstalledAppFlow, "from_client_config", return_value=flow)
    with caplog.at_level("DEBUG"):
        GoogleCredentialsProvider(configuration).authorize()
    assert logger.disabled == previous
    assert "secret_fake" not in caplog.text
    assert "access_fake" not in caplog.text
    assert "refresh_fake" not in caplog.text
    assert str(configuration.token_path) not in caplog.text
    assert "Google authorization completed" in caplog.text


@pytest.mark.parametrize("field", ["excel_path", "whatsapp_manifest_path"])
def test_token_no_can_occupy_file_of_data(configuration: Config, field: str) -> None:
    with pytest.raises(ConfigurationError):
        GoogleCredentialsProvider(
            configuration.model_copy(update={"token_path": getattr(configuration, field)})
        )


@pytest.mark.parametrize("scopes", [(), (SCOPE, SCOPE), ("invented",)])
def test_scopes_of_configuration_must_be_valid(tmp_path: Path, scopes: tuple[str, ...]) -> None:
    with pytest.raises(ValidationError):
        Config(base_directory=tmp_path, google_scopes=scopes, _env_file=None)


def test_configuration_load_scopes_and_profile_start_date_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CA_GOOGLE_AUTHENTICATION_TYPE", "service_account")
    monkeypatch.setenv(
        "CA_GOOGLE_SCOPES",
        json.dumps([SCOPE, "https://www.googleapis.com/auth/drive.readonly"]),
    )
    configuration = load_config(path_env=None)
    assert configuration.google_authentication_type == "service_account"
    assert len(configuration.google_scopes) == 2


def test_configuration_normalizes_spaces_of_scopes(tmp_path: Path) -> None:
    configuration = Config(
        base_directory=tmp_path, google_scopes=(" " + SCOPE + " ",), _env_file=None
    )
    assert configuration.google_scopes == (SCOPE,)


@pytest.mark.parametrize("access", [123, "", "   "])
@pytest.mark.parametrize("refreshed", [False, True])
def test_access_invalid_no_returns_nor_overwrites_token(
    configuration: Config,
    token: Object,
    mocker: MockerFixture,
    access: JsonValue,
    refreshed: bool,
) -> None:
    if refreshed:
        token["expiry"] = "2000-01-01T00:00:00Z"
    else:
        token["token"] = access
    save_json(configuration.token_path, token)
    original = configuration.token_path.read_bytes()
    mocker.patch.object(
        HTTPAdapter, "send", return_value=response(access_token=access, expires_in=3600)
    )
    with pytest.raises(AuthenticationError):
        GoogleCredentialsProvider(configuration).load(persist=True)
    assert configuration.token_path.read_bytes() == original


def test_response_of_refresh_with_scope_malformed_translates(
    configuration: Config, token: Object, mocker: MockerFixture
) -> None:
    token["expiry"] = "2000-01-01T00:00:00Z"
    save_json(configuration.token_path, token)
    original = configuration.token_path.read_bytes()
    mocker.patch.object(
        HTTPAdapter,
        "send",
        return_value=response(access_token="fake", expires_in=3600, scope=[SCOPE]),
    )
    with pytest.raises(AuthenticationError):
        GoogleCredentialsProvider(configuration).load(persist=True)
    assert configuration.token_path.read_bytes() == original


def test_closure_failed_no_hides_error_oauth_original(
    configuration: Config, mocker: MockerFixture
) -> None:
    write_client(configuration)
    flow = mocker.Mock()
    flow.run_local_server.side_effect = RuntimeError("failure fake")
    flow.oauth2session.close.side_effect = OSError("failure fake on close")
    mocker.patch.object(InstalledAppFlow, "from_client_config", return_value=flow)
    with pytest.raises(AuthenticationError) as error:
        GoogleCredentialsProvider(configuration).authorize()
    assert isinstance(error.value.__cause__, RuntimeError)
    assert not configuration.token_path.exists()


@pytest.mark.parametrize("refresh", [False, True])
def test_credentials_loaded_authorize_reader_sheets_real_with_http_simulated(
    configuration: Config, token: Object, mocker: MockerFixture, refresh: bool
) -> None:
    configuration = configuration.model_copy(update={"spreadsheet_id": "document_synthetic"})
    if refresh:
        token["expiry"] = "2000-01-01T00:00:00Z"
    save_json(configuration.token_path, token)
    document = {
        "spreadsheetId": "document_synthetic",
        "sheets": [
            {
                "properties": {
                    "title": name,
                    "sheetId": catalog_index,
                    "gridProperties": {"rowCount": 2, "columnCount": 5},
                },
                "data": [
                    {"rowData": [{"values": [{"userEnteredValue": {"stringValue": "Client"}}]}]}
                ],
            }
            for catalog_index, name in enumerate(("Inviu", "IOL", "Bull", "Balanz", "PPI"))
        ],
    }
    response_sheets = response(spreadsheetId=document["spreadsheetId"], sheets=document["sheets"])
    send = mocker.patch.object(
        HTTPAdapter,
        "send",
        side_effect=[response(), response_sheets] if refresh else [response_sheets],
    )
    credential = GoogleCredentialsProvider(configuration).load()
    with create_reader_sheets(configuration, credential) as reader:
        assert len(reader.revision()) == 64
        assert len(reader.list_sheets()) == 5
    assert send.call_count == (2 if refresh else 1)
    request = send.call_args.args[0]
    assert request.method == "GET"
    assert request.headers["Authorization"] == f"Bearer {credential.token}"
