"""OAuth and service credentials without implicit login or secret logging."""

import json
import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Final, Protocol, cast

from google.auth.credentials import Credentials
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials as UserCredentials
from google.oauth2.service_account import Credentials as ServiceCredentials
from google_auth_oauthlib.flow import InstalledAppFlow
from requests import PreparedRequest, Response, Session
from requests.adapters import HTTPAdapter
from requests.exceptions import ConnectionError, ConnectTimeout, RequestException, SSLError, Timeout
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_random_exponential

from client_automation.config import Config
from client_automation.core.errors import (
    AuthenticationError,
    AuthorizationError,
    AutomationError,
    ConcurrencyError,
    ConfigurationError,
    ExternalServiceError,
    OperationTimeoutError,
    PersistenceError,
    StructureError,
    TransientError,
)
from client_automation.infra.json_storage import JsonValue, read_json, save_json

LOGGER = logging.getLogger(__name__)
URI_TOKEN: Final[str] = "https://oauth2.googleapis.com/token"
URI_AUTHORIZATION: Final[frozenset[str]] = frozenset(
    {
        "https://accounts.google.com/o/oauth2/auth",
        "https://accounts.google.com/o/oauth2/v2/auth",
    }
)
type Object = dict[str, JsonValue]


class _CredentialState(Protocol):
    @property
    def valid(self) -> bool: ...
    @property
    def scopes(self) -> Sequence[str] | None: ...
    @property
    def granted_scopes(self) -> Sequence[str] | None: ...
    def refresh(self, request: Request) -> None: ...
    def to_json(self) -> str: ...


class _OAuthFlow(Protocol):
    oauth2session: Session

    def run_local_server(
        self,
        *,
        host: str,
        bind_addr: str,
        port: int,
        authorization_prompt_message: str,
        success_message: str,
        open_browser: bool,
        timeout_seconds: int,
    ) -> Credentials: ...


class _FlowFactory(Protocol):
    def __call__(
        self,
        client_config: Mapping[str, object],
        scopes: Sequence[str],
        *,
        autogenerate_code_verifier: bool,
    ) -> _OAuthFlow: ...


class _InitialConnectionError(OperationTimeoutError):
    """The connection was not established and the POST has not been sent."""


class _ServiceFactory(Protocol):
    def __call__(
        self,
        info: Mapping[str, object],
        *,
        scopes: Sequence[str],
    ) -> Credentials: ...


class _TokenAdapter(HTTPAdapter):
    def __init__(self, configuration: Config, *, exchange: bool) -> None:
        super().__init__(max_retries=0)
        self._config = configuration
        self._exchange = exchange

    def send(
        self,
        request: PreparedRequest,
        stream: bool = False,
        timeout: float | tuple[float, float] | tuple[float, None] | None = None,
        verify: bool | str = True,
        cert: bytes | str | tuple[bytes | str, bytes | str] | None = None,
        proxies: Mapping[str, str] | None = None,
    ) -> Response:
        if request.url != URI_TOKEN or request.method != "POST":
            raise AuthenticationError("Authentication requires Google's token endpoint.")
        for attempt in Retrying(
            retry=retry_if_exception(self._retry),
            stop=stop_after_attempt(self._config.max_attempts),
            wait=wait_random_exponential(
                multiplier=self._config.retry_min_wait_seconds,
                min=self._config.retry_min_wait_seconds,
                max=self._config.retry_max_wait_seconds,
            ),
            reraise=True,
        ):
            with attempt:
                try:
                    response = super().send(
                        request,
                        stream=stream,
                        timeout=self._config.network_timeout_seconds,
                        verify=verify,
                        cert=cert,
                        proxies=proxies,
                    )
                except SSLError as cause:
                    raise ExternalServiceError(
                        "TLS could not be verified during Google authentication."
                    ) from cause
                except ConnectTimeout as cause:
                    raise _InitialConnectionError(
                        "The authentication server could not be reached."
                    ) from cause
                except Timeout as cause:
                    raise OperationTimeoutError(
                        "Authentication exceeded the permitted timeout."
                    ) from cause
                except ConnectionError as cause:
                    raise TransientError(
                        "The authentication connection failed temporarily."
                    ) from cause
                except RequestException as cause:
                    raise ExternalServiceError(
                        "Google authentication could not be completed."
                    ) from cause
                if 200 <= response.status_code < 300:
                    return response
                response.close()
                if response.status_code == 408:
                    raise OperationTimeoutError("Google authentication timed out.")
                if response.status_code == 429 or (
                    500 <= response.status_code < 600 and response.status_code not in {501, 505}
                ):
                    raise TransientError("The authentication server is temporarily busy.")
                if response.status_code == 403:
                    raise AuthorizationError("Google rejected the authentication permissions.")
                raise AuthenticationError(
                    "Google rejected authentication. Review the authorization."
                )
        raise ExternalServiceError("Google authentication could not be finished.")

    def _retry(self, cause: BaseException) -> bool:
        allowed = (
            isinstance(cause, _InitialConnectionError)
            if self._exchange
            else isinstance(cause, TransientError)
        )
        if allowed:
            LOGGER.warning("Temporary authentication failure; another attempt will be considered.")
        return allowed


@contextmanager
def _silence_sdk() -> Iterator[None]:
    # Authentication SDKs may log OAuth codes and tokens when external DEBUG is enabled.
    prefixes = (
        "oauthlib",
        "requests_oauthlib",
        "google_auth_oauthlib",
        "google.auth",
        "google.oauth2",
    )
    previous = [
        (logger, logger.disabled)
        for name, logger in tuple(logging.Logger.manager.loggerDict.items())
        if isinstance(logger, logging.Logger) and name.startswith(prefixes)
    ]
    for logger, _ in previous:
        logger.disabled = True
    try:
        yield
    finally:
        for logger, disabled in previous:
            logger.disabled = disabled


@dataclass(frozen=True, slots=True)
class _ReadToken:
    document: Object | None = field(repr=False)


class GoogleCredentialsProvider:
    """Load credentials; initial authorization requires an explicit method call."""

    def __init__(self, configuration: Config) -> None:
        """Accept immutable settings and validate destinations without opening secrets."""
        self._config = configuration
        protected = (
            configuration.credentials_path,
            configuration.excel_path,
            configuration.contacts_path,
            configuration.whatsapp_manifest_path,
            configuration.catalog_path,
            configuration.aliases_path,
            configuration.exclusion_list_path,
            configuration.resolution_path,
        )
        if any(
            configuration.token_path.is_relative_to(path)
            or path.is_relative_to(configuration.token_path)
            for path in protected
        ):
            raise ConfigurationError("The token path must be separate from all other files.")

    def load(self, *, refresh: bool = True, persist: bool = False) -> Credentials:
        """Load OAuth or service credentials and refresh with bounded timeouts if needed."""
        initial = _ReadToken(None)
        if self._config.google_authentication_type == "service_account":
            credentials = self._load_service()
        else:
            initial = self._read_token(optional=True)
            if initial.document is None:
                raise AuthenticationError("An OAuth token is missing. Authorize Google explicitly.")
            self._validate_token(initial.document)
            make = cast(
                Callable[[Mapping[str, object]], Credentials],
                UserCredentials.from_authorized_user_info,
            )
            try:
                credentials = make(initial.document)
            except (ValueError, TypeError, GoogleAuthError) as cause:
                raise AuthenticationError("The OAuth token has an invalid format.") from cause
        self._validate_scopes(credentials)
        state = cast(_CredentialState, credentials)
        refreshed = not state.valid
        if refreshed:
            if not refresh:
                raise AuthenticationError("The credentials require an explicit refresh.")
            with Session() as session:
                session.mount("https://", _TokenAdapter(self._config, exchange=False))
                with _silence_sdk():
                    try:
                        state.refresh(Request(session=session))
                    except AutomationError:
                        raise
                    except (
                        GoogleAuthError,
                        ValueError,
                        TypeError,
                        AttributeError,
                        KeyError,
                        OverflowError,
                    ) as cause:
                        raise AuthenticationError(
                            "Google authorization could not be refreshed."
                        ) from cause
            if not state.valid:
                raise AuthenticationError("Refreshing did not produces valid credentials.")
            _text(credentials.token)
            self._validate_scopes(credentials)
            if persist and self._config.google_authentication_type == "oauth":
                self._save_token(credentials, initial)
        LOGGER.info("Google credentials available: refreshed=%s.", refreshed)
        return credentials

    def authorize(self, *, persist: bool = True) -> Credentials:
        """Authorize desktop OAuth using a browser and a local IPv4 callback."""
        if self._config.google_authentication_type != "oauth":
            raise ConfigurationError(
                "Interactive authorization requires the OAuth authentication type."
            )
        initial = self._read_token(optional=True)
        document = _object(read_json(self._config.credentials_path))
        installed = _object(document.get("installed"))
        if (
            not isinstance(installed.get("auth_uri"), str)
            or installed.get("auth_uri") not in URI_AUTHORIZATION
            or installed.get("token_uri") != URI_TOKEN
        ):
            raise AuthenticationError("Configure a valid Google desktop OAuth client.")
        for field_name in ("client_id", "client_secret"):
            _text(installed.get(field_name))
        make = cast(_FlowFactory, InstalledAppFlow.from_client_config)
        flow: _OAuthFlow | None = None
        with _silence_sdk():
            try:
                flow = make(document, self._config.google_scopes, autogenerate_code_verifier=True)
                flow.oauth2session.mount("https://", _TokenAdapter(self._config, exchange=True))
                LOGGER.info("Google authorization started in the browser.")
                credentials = flow.run_local_server(
                    host="127.0.0.1",
                    bind_addr="127.0.0.1",
                    port=0,
                    authorization_prompt_message="",
                    success_message="Authorization completed. You may close this window.",
                    open_browser=True,
                    timeout_seconds=self._config.google_authorization_timeout_seconds,
                )
            except AutomationError:
                raise
            except Exception as cause:
                raise AuthenticationError(
                    "Google OAuth authorization was not completed."
                ) from cause
            finally:
                if flow is not None:
                    try:
                        flow.oauth2session.close()
                    except OSError:
                        LOGGER.warning("The Google OAuth session could not be closed.")
        if not cast(_CredentialState, credentials).valid:
            raise AuthenticationError("Authorization did not produces valid credentials.")
        _text(credentials.token)
        self._validate_scopes(credentials)
        if persist:
            self._save_token(credentials, initial)
        LOGGER.info("Google authorization completed: token saved=%s.", persist)
        return credentials

    def _load_service(self) -> Credentials:
        document = _object(read_json(self._config.credentials_path))
        if document.get("type") != "service_account" or document.get("token_uri") != URI_TOKEN:
            raise AuthenticationError("Configure a valid Google service account file.")
        for field_name in ("client_email", "private_key"):
            _text(document.get(field_name))
        make = cast(_ServiceFactory, ServiceCredentials.from_service_account_info)
        try:
            return make(document, scopes=self._config.google_scopes)
        except (GoogleAuthError, ValueError, TypeError) as cause:
            raise AuthenticationError("The service account key is invalid.") from cause

    def _read_token(self, *, optional: bool) -> _ReadToken:
        try:
            return _ReadToken(_object(read_json(self._config.token_path)))
        except PersistenceError as cause:
            if optional and isinstance(cause.__cause__, FileNotFoundError):
                return _ReadToken(None)
            raise

    def _validate_token(self, document: Object) -> None:
        if document.get("token") is not None:
            _text(document["token"])
        if document.get("type", "authorized_user") != "authorized_user":
            raise AuthenticationError("The token file must contain user OAuth credentials.")
        if document.get("token_uri", URI_TOKEN) != URI_TOKEN:
            raise AuthenticationError("The token does not use the supported Google endpoint.")
        for field_name in ("client_id", "client_secret", "refresh_token"):
            _text(document.get(field_name))
        scopes: object = document.get("scopes")
        if isinstance(scopes, str):
            scopes = scopes.split()
        if (
            not isinstance(scopes, list)
            or not scopes
            or any(not isinstance(value, str) or not value.strip() for value in scopes)
        ):
            raise AuthorizationError("The token must declare its authorization scopes.")

    def _validate_scopes(self, credentials: Credentials) -> None:
        state = cast(_CredentialState, credentials)
        granted: object = getattr(credentials, "granted_scopes", None)
        available = granted if granted is not None else state.scopes
        if not isinstance(available, (list, tuple)) or any(
            not isinstance(value, str) for value in available
        ):
            raise AuthorizationError("Google authorization scopes could not be verified.")
        if not set(self._config.google_scopes) <= set(available):
            raise AuthorizationError("Google authorization does not include the required scopes.")

    def _save_token(self, credentials: Credentials, previous: _ReadToken) -> None:
        try:
            serialize = cast(_CredentialState, credentials).to_json
            document = _object(json.loads(serialize()))
        except (ValueError, TypeError) as cause:
            raise AuthenticationError("The Google token could not be serialized.") from cause
        granted: object = getattr(credentials, "granted_scopes", None)
        if granted is not None:
            document["scopes"] = list(cast(Sequence[str], granted))
        self._validate_token(document)
        if self._read_token(optional=True) != previous:
            raise ConcurrencyError("The token changed during authentication. Open a new session.")
        save_json(self._config.token_path, document)


def _object(value: object) -> Object:
    if not isinstance(value, dict):
        raise StructureError("The authentication file must contain a JSON object.")
    return cast(Object, value)


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AuthenticationError("A required Google credential field is missing.")
    return value
