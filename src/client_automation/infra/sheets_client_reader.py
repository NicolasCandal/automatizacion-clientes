"""Google Sheets read snapshots with bounded retries and safe errors."""

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from math import isfinite
from types import TracebackType
from typing import Final, Protocol, Self, cast
from urllib.parse import quote

from google.auth.credentials import Credentials
from google.auth.exceptions import GoogleAuthError, RefreshError, TransportError
from google.auth.transport.requests import AuthorizedSession
from google.auth.transport.requests import Request as CredentialsRequest
from requests import Response, Session
from requests.exceptions import (
    ChunkedEncodingError,
    ConnectionError,
    RequestException,
    SSLError,
    Timeout,
)
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from client_automation.config import Config
from client_automation.core.errors import (
    AuthenticationError,
    AuthorizationError,
    ConfigurationError,
    ExternalServiceError,
    OperationTimeoutError,
    StructureError,
    TransientError,
)
from client_automation.core.input_formats import CLIENT_COLUMN_HEADERS
from client_automation.core.models import ClientRow, SheetInfo

LOGGER = logging.getLogger(__name__)
EXPECTED_SHEETS: Final[tuple[str, ...]] = ("Inviu", "IOL", "Bull", "Balanz", "PPI")
READ_FIELDS: Final[str] = (
    "spreadsheetId,properties(spreadsheetTheme,defaultFormat),sheets(properties,data,merges,conditionalFormats,protectedRanges,bandedRanges,tables)"
)
type Object = dict[str, object]


class SheetsSession(Protocol):
    """Injectable authorized transport without access to credential files."""

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str | list[str]],
        timeout: float,
        allow_redirects: bool,
    ) -> Response:
        """Perform one request with an explicit timeout."""
        ...

    def close(self) -> None:
        """Release session connections when requested by their owner."""
        ...


class _SessionFactory(Protocol):
    def __call__(
        self,
        credentials: Credentials,
        *,
        max_refresh_attempts: int,
        refresh_timeout: float,
        auth_request: CredentialsRequest,
    ) -> SheetsSession: ...


@dataclass(slots=True)
class _OwnedSession:
    authorized: SheetsSession
    refresh: Session

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str | list[str]],
        timeout: float,
        allow_redirects: bool,
    ) -> Response:
        return self.authorized.get(
            url,
            params=params,
            timeout=timeout,
            allow_redirects=allow_redirects,
        )

    def close(self) -> None:
        try:
            self.authorized.close()
        finally:
            self.refresh.close()


@dataclass(frozen=True, slots=True)
class _ReadSheet:
    info: SheetInfo
    clients: tuple[ClientRow, ...]
    headers: tuple[tuple[int, str], ...]
    marks: dict[int, frozenset[int]]
    colors_no_resolved: frozenset[int]


class SheetsClientReader:
    """Implement RegistrationClientReader with a five-sheet snapshot."""

    def __init__(
        self,
        spreadsheet_id: str,
        *,
        session: SheetsSession,
        timeout_seconds: float = 30.0,
        attempts: int = 4,
        wait_minimum_seconds: float = 1.0,
        wait_maximum_seconds: float = 30.0,
        close_session: bool = False,
    ) -> None:
        """Configure reading without network, file, discovery, or authentication I/O."""
        if not spreadsheet_id.strip():
            raise ConfigurationError("A Google Sheets document identifier is required.")
        if type(attempts) is not int or not 1 <= attempts <= 10:
            raise ConfigurationError("Configure between one and ten read attempts.")
        for value in (timeout_seconds, wait_minimum_seconds, wait_maximum_seconds):
            if isinstance(value, bool) or not isfinite(value) or value <= 0:
                raise ConfigurationError("Read timeouts and waits must be positive and finite.")
        if wait_minimum_seconds > wait_maximum_seconds:
            raise ConfigurationError("The minimum wait cannot exceed the maximum.")
        self._identifier = spreadsheet_id.strip()
        self._url = "https://sheets.googleapis.com/v4/spreadsheets/" + quote(
            self._identifier, safe=""
        )
        self._session = session
        self._timeout = timeout_seconds
        self._attempts = attempts
        self._wait_minimum = wait_minimum_seconds
        self._wait_maximum = wait_maximum_seconds
        self._close_session = close_session
        self._closed = False
        self._sheets: dict[str, _ReadSheet] | None = None

    def list_sheets(self) -> tuple[SheetInfo, ...]:
        """Return the five required sheets in stable order with their current IDs."""
        return tuple(sheet.info for sheet in self._get_sheets().values())

    def read_clients(self, sheet: str) -> tuple[ClientRow, ...]:
        """Preserve original names and all client rows, including gaps."""
        return self._get_sheet(sheet).clients

    def read_headers(self, sheet: str) -> tuple[tuple[int, str], ...]:
        """Return every weekly column and its untrimmed header text."""
        return self._get_sheet(sheet).headers

    def last_week(self, sheet: str) -> tuple[int, str]:
        """Return the last week or (1, '') when no weekly headers exist."""
        headers = self._get_sheet(sheet).headers
        return headers[-1] if headers else (1, "")

    def read_marks(self, sheet: str, column: int) -> frozenset[int]:
        """Recognize effective green fills or literal x markers only on client rows."""
        if type(column) is not int or column < 1:
            raise StructureError("The mark column must be a positive integer.")
        read = self._get_sheet(sheet)
        first, last = read.info.first_week_column, read.info.last_week_column
        if first is None or last is None or not first <= column <= last:
            raise StructureError("The column does not belong to existing weeks.")
        if column in read.colors_no_resolved:
            raise StructureError("A weekly mark's theme color could not be resolved.")
        return read.marks.get(column, frozenset())

    def revision(self) -> str:
        """Refresh from the API and hash cell values and formatting with SHA-256."""
        return self.read_snapshot()[0]

    def read_snapshot(self) -> tuple[str, dict[str, object]]:
        """Download and validate a fresh snapshot for backups and preconditions."""
        self._ensure_open()
        self._sheets = None
        document = self._query()
        try:
            content = json.dumps(
                document,
                sort_keys=True,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (ValueError, TypeError, RecursionError) as cause:
            raise StructureError("The Sheets response does not contain compatible JSON.") from cause
        sheets = _parse_document(document, self._identifier)
        revision = sha256(content).hexdigest()
        self._sheets = sheets
        LOGGER.debug("Sheets snapshot validated: %s sheets.", len(sheets))
        return revision, cast(dict[str, object], document)

    def close(self) -> None:
        """Invalidate the reader and close only a session owned by this reader."""
        if self._closed:
            return
        self._closed = True
        self._sheets = None
        if self._close_session:
            try:
                self._session.close()
            except OSError as cause:
                raise ExternalServiceError("The Sheets session could not be closed.") from cause

    def __enter__(self) -> Self:
        """Manage the snapshot and any owned session with a context manager."""
        self._ensure_open()
        return self

    def __exit__(
        self,
        kind_error: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close resources without hiding an exception raised inside the block."""
        try:
            self.close()
        except ExternalServiceError:
            if error is None:
                raise
            LOGGER.warning("The Sheets session could not be closed after an earlier failure.")

    def _ensure_open(self) -> None:
        if self._closed:
            raise ConfigurationError("The Sheets reader is closed. Create a new instance.")

    def _get_sheets(self) -> dict[str, _ReadSheet]:
        self._ensure_open()
        if self._sheets is None:
            self.revision()
        if self._sheets is None:
            raise StructureError("The Sheets snapshot could not be constructed.")
        return self._sheets

    def _get_sheet(self, name: str) -> _ReadSheet:
        if name not in EXPECTED_SHEETS:
            raise StructureError("The requested sheet is not a configured client sheet.")
        return self._get_sheets()[name]

    def _query(self) -> object:
        for attempt in Retrying(
            retry=retry_if_exception_type(TransientError),
            stop=stop_after_attempt(self._attempts),
            wait=wait_random_exponential(
                multiplier=self._wait_minimum, min=self._wait_minimum, max=self._wait_maximum
            ),
            before_sleep=_warn_retry,
            reraise=True,
        ):
            with attempt:
                return self._download()
        raise ExternalServiceError("The Sheets read could not be completed.")

    def _download(self) -> object:
        try:
            response = self._session.get(
                self._url,
                params={
                    "ranges": [f"'{name}'" for name in EXPECTED_SHEETS],
                    "fields": READ_FIELDS,
                },
                timeout=self._timeout,
                allow_redirects=False,
            )
            try:
                validate_state_http_sheets(response)
                try:
                    document: object = response.json()
                except ValueError as cause:
                    raise StructureError(
                        "The Sheets response does not contain valid JSON."
                    ) from cause
                return document
            finally:
                response.close()
        except SSLError as cause:
            raise ExternalServiceError(
                "The Sheets TLS connection could not be verified."
            ) from cause
        except Timeout as cause:
            raise OperationTimeoutError(
                "The Sheets read exceeded the permitted timeout."
            ) from cause
        except (ConnectionError, ChunkedEncodingError, TransportError) as cause:
            raise TransientError("The Sheets connection failed temporarily.") from cause
        except RefreshError as cause:
            if getattr(cause, "retryable", False) is True:
                raise TransientError("Credential refreshing failed temporarily.") from cause
            raise AuthenticationError("Google credentials require refreshing.") from cause
        except GoogleAuthError as cause:
            raise AuthenticationError("The Google Sheets read could not be authorized.") from cause
        except RequestException as cause:
            raise ExternalServiceError("The Sheets query could not be completed.") from cause


def create_reader_sheets(configuration: Config, credentials: Credentials) -> SheetsClientReader:
    """Create an owned session from already loaded credentials without file access."""
    if configuration.spreadsheet_id is None:
        raise ConfigurationError("Configure the Google Sheets document identifier.")
    # google-auth's constructor is untyped; constrain only this external boundary.
    make_session = cast(_SessionFactory, AuthorizedSession)
    # Session adapters disable retries so credential refresh cannot multiply attempts.
    refresh = Session()
    try:
        session = make_session(
            credentials,
            max_refresh_attempts=0,
            refresh_timeout=configuration.network_timeout_seconds,
            auth_request=CredentialsRequest(session=refresh),
        )
    except Exception:
        refresh.close()
        raise
    return SheetsClientReader(
        configuration.spreadsheet_id,
        session=_OwnedSession(authorized=session, refresh=refresh),
        close_session=True,
        timeout_seconds=configuration.network_timeout_seconds,
        attempts=configuration.max_attempts,
        wait_minimum_seconds=configuration.retry_min_wait_seconds,
        wait_maximum_seconds=configuration.retry_max_wait_seconds,
    )


def validate_state_http_sheets(response: Response) -> None:
    """Translate HTTP failures without exposing response bodies, IDs, or secrets."""
    state = response.status_code
    if 200 <= state < 300:
        return
    if state == 408:
        raise OperationTimeoutError("The Sheets API query timed out.")
    if state == 429 or (500 <= state < 600 and state not in {501, 505}):
        raise TransientError("The Sheets API is temporarily unavailable.")
    if state == 401:
        raise AuthenticationError("Google Sheets rejected the credentials.")
    if state == 403:
        try:
            content: object = response.json()
        except ValueError:
            content = None
        if _is_quota_temporary(content):
            raise TransientError("The Sheets read quota is temporarily exhausted.")
        raise AuthorizationError("The identity is not authorized to read Sheets.")
    if state == 404:
        raise AuthorizationError("The requested spreadsheet does not exist or is inaccessible.")
    if state == 400:
        raise StructureError("The Sheets API rejected the query structure.")
    raise ExternalServiceError("The Sheets API rejected the query.")


def _is_quota_temporary(content: object) -> bool:
    if not isinstance(content, dict):
        return False
    error = cast(dict[str, object], content).get("error")
    if not isinstance(error, dict):
        return False
    reasons = cast(dict[str, object], error).get("errors")
    if not isinstance(reasons, list):
        return False
    reasons = [
        cast(dict[str, object], reason).get("reason")
        for reason in reasons
        if isinstance(reason, dict)
    ]
    return bool(reasons) and all(
        isinstance(reason, str) and reason in {"rateLimitExceeded", "userRateLimitExceeded"}
        for reason in reasons
    )


def _warn_retry(state: RetryCallState) -> None:
    LOGGER.warning(
        "Transient Sheets read failure; retrying after attempt %s.", state.attempt_number
    )


def _object(value: object) -> Object:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise StructureError("Expected an object in the Sheets response.")
    return cast(Object, value)


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise StructureError("Expected a list in the Sheets response.")
    return cast(list[object], value)


def _integer(value: object, *, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise StructureError("A Sheets index or dimension is invalid.")
    return value


def _parse_document(document: object, identifier: str) -> dict[str, _ReadSheet]:
    root = _object(document)
    if root.get("spreadsheetId") != identifier:
        raise StructureError("The response does not match the requested spreadsheet.")
    sheets: dict[str, Object] = {}
    identifiers: set[int] = set()
    for element in _list(root.get("sheets")):
        sheet = _object(element)
        properties = _object(sheet.get("properties"))
        name = properties.get("title")
        if not isinstance(name, str):
            raise StructureError("The Sheets response contains a sheet without a valid name.")
        if name in sheets:
            raise StructureError("The Sheets response contains duplicate sheet names.")
        identifier_sheet = _integer(properties.get("sheetId"), minimum=0)
        if identifier_sheet in identifiers:
            raise StructureError("The Sheets response contains duplicate identifiers.")
        identifiers.add(identifier_sheet)
        sheets[name] = sheet
    if not set(EXPECTED_SHEETS) <= sheets.keys():
        raise StructureError("The spreadsheet lacks required client sheets.")
    themes = _get_themes(root)
    format_default = _object(_object(root.get("properties", {})).get("defaultFormat", {}))
    return {name: _parse_sheet(sheets[name], themes, format_default) for name in EXPECTED_SHEETS}


def _parse_sheet(
    sheet: Object,
    themes: Mapping[str, tuple[float, float, float]],
    format_default: Object,
) -> _ReadSheet:
    properties = _object(sheet.get("properties"))
    name = properties["title"]
    if not isinstance(name, str) or properties.get("sheetType", "GRID") != "GRID":
        raise StructureError("Client sheets must be tabular grids.")
    dimensions = _object(properties.get("gridProperties"))
    total_rows = _integer(dimensions.get("rowCount"), minimum=2)
    total_columns = _integer(dimensions.get("columnCount"), minimum=1)
    cells = _get_cells(sheet, total_rows, total_columns)
    title = _text_literal(cells.get((1, 1), {}), name=True)
    if title is None or title.strip().casefold() not in CLIENT_COLUMN_HEADERS:
        raise StructureError("Column A must have a valid client header.")
    clients: list[ClientRow] = []
    headers: list[tuple[int, str]] = []
    first_column = 4 if name == "Balanz" else 3
    for (row, column), cell in sorted(cells.items()):
        if row == 2 and column >= first_column:
            text = _text_literal(cell, name=False)
            if text is not None and text.strip():
                headers.append((column, text))
        elif row >= 3 and column == 1:
            text = _text_literal(cell, name=True)
            if text is not None and text.strip():
                clients.append(ClientRow(row=row, raw_name=text))
    first = headers[0][0] if headers else None
    last = headers[-1][0] if headers else None
    client_rows = {client.row for client in clients}
    marks: dict[int, set[int]] = {}
    unresolved: set[int] = set()
    for (row, column), cell in cells.items():
        if row not in client_rows or first is None or last is None or not first <= column <= last:
            continue
        mark = _classify_mark(cell, themes, format_default)
        if mark is None:
            unresolved.add(column)
        elif mark:
            marks.setdefault(column, set()).add(row)
    return _ReadSheet(
        info=SheetInfo(
            name=name,
            sheet_id=_integer(properties["sheetId"], minimum=0),
            name_column=1,
            first_week_column=first,
            last_week_column=last,
            last_client_row=clients[-1].row if clients else 2,
        ),
        clients=tuple(clients),
        headers=tuple(headers),
        marks={column: frozenset(rows) for column, rows in marks.items()},
        colors_no_resolved=frozenset(unresolved),
    )


def _get_cells(sheet: Object, total_rows: int, total_columns: int) -> dict[tuple[int, int], Object]:
    cells: dict[tuple[int, int], Object] = {}
    for element in _list(sheet.get("data", [])):
        block = _object(element)
        start_row = _integer(block.get("startRow", 0), minimum=0)
        start_column = _integer(block.get("startColumn", 0), minimum=0)
        if start_row >= total_rows or start_column >= total_columns:
            raise StructureError("A Sheets block starts outside the grid.")
        for offset_row, element_row in enumerate(_list(block.get("rowData", []))):
            row = start_row + offset_row + 1
            row_raw = _object(element_row)
            if row > total_rows:
                raise StructureError("A Sheets row exceeds its declared dimensions.")
            for offset_column, element_cell in enumerate(_list(row_raw.get("values", []))):
                column = start_column + offset_column + 1
                if column > total_columns or (row, column) in cells:
                    raise StructureError("A Sheets cell is duplicated or outside the grid.")
                cells[(row, column)] = _object(element_cell)
    return cells


def _text_literal(cell: Object, *, name: bool) -> str | None:
    value = _object(cell.get("userEnteredValue", {}))
    effective = _object(cell.get("effectiveValue", {}))
    if "formulaValue" in value or "errorValue" in value or "errorValue" in effective:
        raise StructureError("Client names and headers must be literal values.")
    if not value:
        if effective:
            raise StructureError("A client or header cell lacks a literal value.")
        return None
    if len(value) != 1 or not set(value) <= {"stringValue", "numberValue", "boolValue"}:
        raise StructureError("A Sheets literal value has an unexpected type.")
    if "stringValue" in value:
        text = value["stringValue"]
        if not isinstance(text, str):
            raise StructureError("A Sheets text cell has an invalid value.")
        return text
    if name:
        raise StructureError("Client names must be literal text.")
    if "boolValue" in value and type(value["boolValue"]) is not bool:
        raise StructureError("A Sheets boolean header has an invalid value.")
    if "numberValue" in value:
        number = value["numberValue"]
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            raise StructureError("A Sheets numeric header has an invalid value.")
        try:
            finite = isfinite(number)
        except OverflowError as cause:
            raise StructureError("A Sheets numeric header exceeds the supported range.") from cause
        if not finite:
            raise StructureError("A Sheets numeric header must be finite.")
    formatted = cell.get("formattedValue")
    if formatted is not None:
        if not isinstance(formatted, str):
            raise StructureError("A formatted Sheets header must be text.")
        return formatted
    return str(next(iter(value.values())))


def _color_rgb(value: object) -> tuple[float, float, float]:
    color = _object(value)
    channels: list[float] = []
    for channel in ("red", "green", "blue"):
        component = color.get(channel, 0.0)
        if (
            isinstance(component, bool)
            or not isinstance(component, (int, float))
            or not 0 <= component <= 1
            or not isfinite(component)
        ):
            raise StructureError("A Sheets color component is invalid.")
        channels.append(float(component))
    return channels[0], channels[1], channels[2]


def _get_themes(root: Object) -> dict[str, tuple[float, float, float]]:
    properties = _object(root.get("properties", {}))
    theme = _object(properties.get("spreadsheetTheme", {}))
    themes: dict[str, tuple[float, float, float]] = {}
    for element in _list(theme.get("themeColors", [])):
        pair = _object(element)
        kind = pair.get("colorType")
        color = _object(pair.get("color"))
        if (
            not isinstance(kind, str)
            or not kind.strip()
            or kind in themes
            or "rgbColor" not in color
        ):
            raise StructureError("The Sheets theme color table is invalid.")
        themes[kind] = _color_rgb(color["rgbColor"])
    return themes


def _classify_mark(
    cell: Object,
    themes: Mapping[str, tuple[float, float, float]],
    format_default: Object,
) -> bool | None:
    literal = _object(cell.get("userEnteredValue", {})).get("stringValue")
    if isinstance(literal, str) and literal.strip().casefold() == "x":
        return True
    format = _object(cell.get("effectiveFormat", {}))
    if not {"backgroundColorStyle", "backgroundColor"} & format.keys():
        format = format_default
    if "backgroundColorStyle" in format:
        style = _object(format["backgroundColorStyle"])
        if "rgbColor" in style and "themeColor" in style:
            raise StructureError("A Sheets color cannot have two representations.")
        if "rgbColor" in style:
            color = _color_rgb(style["rgbColor"])
        elif "themeColor" in style:
            kind = style["themeColor"]
            if not isinstance(kind, str):
                raise StructureError("A Sheets theme color name is invalid.")
            if kind not in themes:
                return None
            color = themes[kind]
        else:
            return False
    elif "backgroundColor" in format:
        color = _color_rgb(format["backgroundColor"])
    else:
        return False
    green = (118 / 255, 146 / 255, 60 / 255)
    return all(
        abs(channel - expected) <= 0.01 for channel, expected in zip(color, green, strict=True)
    )
