"""Local Sheets backups and idempotent, revision-checked write batches."""

import json
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC
from os import fsync
from pathlib import Path
from tempfile import NamedTemporaryFile
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
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_random_exponential

from client_automation.config import Config
from client_automation.core.contracts import Clock
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
from client_automation.infra.sheets_client_reader import (
    EXPECTED_SHEETS,
    SheetsClientReader,
    SheetsSession,
    validate_state_http_sheets,
)

LOGGER = logging.getLogger(__name__)
MAXIMUM_COLUMN: Final[int] = 18278
WRITE_SCOPES: Final[frozenset[str]] = frozenset(
    {
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.file",
        "https://www.googleapis.com/auth/drive",
    }
)
type Object = dict[str, object]


class SheetsWriteSession(SheetsSession, Protocol):
    """Authorized transport without its own retries or implicit redirects."""

    def post(
        self,
        url: str,
        *,
        json: Mapping[str, object],
        timeout: float,
        allow_redirects: bool,
    ) -> Response:
        """Send a single batch to the Sheets API."""
        ...


class _SessionFactory(Protocol):
    def __call__(
        self,
        credentials: Credentials,
        *,
        max_refresh_attempts: int,
        refresh_timeout: float,
        auth_request: CredentialsRequest,
    ) -> Session: ...


class _ScopedCredentials(Protocol):
    def has_scopes(self, scopes: Sequence[str]) -> bool: ...


class SheetsClientWriter:
    """Implement ClientWriter with a JSON backup and one batch per sheet."""

    def __init__(
        self,
        spreadsheet_id: str,
        *,
        session: SheetsWriteSession,
        backup_directory: Path,
        clock: Clock,
        timeout_seconds: float = 30.0,
        attempts: int = 4,
        wait_minimum_seconds: float = 1.0,
        wait_maximum_seconds: float = 30.0,
        close_session: bool = False,
        session_refresh: Session | None = None,
    ) -> None:
        """Accept dependencies without accessing the network, credentials, or files."""
        self._reader = SheetsClientReader(
            spreadsheet_id,
            session=session,
            timeout_seconds=timeout_seconds,
            attempts=attempts,
            wait_minimum_seconds=wait_minimum_seconds,
            wait_maximum_seconds=wait_maximum_seconds,
        )
        self._session = session
        self._refresh = session_refresh
        self._close_session = close_session
        self._closed = False
        self._identifier = spreadsheet_id.strip()
        self._url = (
            "https://sheets.googleapis.com/v4/spreadsheets/"
            + quote(self._identifier, safe="")
            + ":batchUpdate"
        )
        self._directory_backups = backup_directory
        self._clock = clock
        self._timeout = timeout_seconds
        self._attempts = attempts
        self._wait_minimum = wait_minimum_seconds
        self._wait_maximum = wait_maximum_seconds

    def create_backup(self) -> Path:
        """Save and synchronize the five queried sheets in an exclusive JSON backup."""
        self._ensure_open()
        instant = self._clock.now()
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ConfigurationError("The backup clock must be timezone-aware.")
        instant = instant.astimezone(UTC)
        revision, document = self._reader.read_snapshot()
        content = json.dumps(
            {
                "version": 1,
                "creado_in": instant.isoformat(),
                "revision": revision,
                "document": document,
            },
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
        ).encode("utf-8")
        backup: Path | None = None
        completed = False
        try:
            self._directory_backups.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(
                mode="wb",
                dir=self._directory_backups,
                prefix=instant.strftime("sheets_%Y%m%dT%H%M%S_%fZ_"),
                suffix=".json",
                delete=False,
            ) as file:
                backup = Path(file.name)
                file.write(content)
                file.flush()
                fsync(file.fileno())
            if self._reader.revision() != revision:
                raise ConcurrencyError("Sheets changed during backup creation.")
            completed = True
        except OSError as cause:
            raise PersistenceError("The local Sheets backup could not be saved.") from cause
        finally:
            if backup is not None and not completed:
                try:
                    backup.unlink(missing_ok=True)
                except OSError:
                    LOGGER.warning("An incomplete Sheets backup could not be removed.")
        LOGGER.info("Local Sheets backup created and synchronized.")
        return backup

    def apply_marks(
        self,
        sheet: str,
        column: int,
        header: str,
        rows: frozenset[int],
        *,
        expected_revision: str,
    ) -> None:
        """Assign an existing or next week using one atomic batch per sheet."""
        self._ensure_open()
        _validate_request(sheet, column, header, rows, expected_revision)
        send_started = False
        retry = retry_if_exception(lambda cause: send_started and isinstance(cause, TransientError))
        for attempt in Retrying(
            retry=retry,
            stop=stop_after_attempt(self._attempts),
            wait=wait_random_exponential(
                multiplier=self._wait_minimum, min=self._wait_minimum, max=self._wait_maximum
            ),
            reraise=True,
        ):
            with attempt:
                send_started = False
                revision, document = self._reader.read_snapshot()
                if revision != expected_revision:
                    raise ConcurrencyError("Sheets changed. Read and plan again.")
                requests = _prepare(self._reader, document, sheet, column, header, rows)
                if requests:
                    if self._reader.revision() != expected_revision:
                        raise ConcurrencyError("Sheets changed before the batch was sent.")
                    send_started = True
                    self._send({"requests": requests, "includeSpreadsheetInResponse": False})
        LOGGER.info("Sheets week updated: %s clients marked.", len(rows))

    def _send(self, body: Mapping[str, object]) -> None:
        try:
            response = self._session.post(
                self._url, json=body, timeout=self._timeout, allow_redirects=False
            )
            try:
                validate_state_http_sheets(response)
                try:
                    result = _object(response.json())
                except ValueError as cause:
                    raise StructureError(
                        "Sheets did not confirm the batch with valid JSON."
                    ) from cause
                if result.get("spreadsheetId") != self._identifier:
                    raise StructureError(
                        "Sheets did not confirm the document for the applied batch."
                    )
            finally:
                response.close()
        except SSLError as cause:
            raise ExternalServiceError("TLS could not be verified while writing Sheets.") from cause
        except Timeout as cause:
            LOGGER.warning("Sheets write unconfirmed; the revision will be checked.")
            raise OperationTimeoutError(
                "The Sheets write exceeded the permitted timeout."
            ) from cause
        except (ConnectionError, ChunkedEncodingError, TransportError) as cause:
            LOGGER.warning("Sheets write unconfirmed; the revision will be checked.")
            raise TransientError("The Sheets write connection failed temporarily.") from cause
        except RefreshError as cause:
            if getattr(cause, "retryable", False) is True:
                raise TransientError("Credential refreshing failed temporarily.") from cause
            raise AuthenticationError("Google credentials require refreshing.") from cause
        except GoogleAuthError as cause:
            raise AuthenticationError("The Sheets write could not be authorized.") from cause
        except RequestException as cause:
            raise ExternalServiceError("The Sheets batch could not be completed.") from cause

    def close(self) -> None:
        """Close owned resources once and invalidate the writer."""
        if self._closed:
            return
        self._closed = True
        self._reader.close()
        try:
            try:
                if self._close_session:
                    self._session.close()
            finally:
                if self._refresh is not None:
                    self._refresh.close()
        except OSError as cause:
            raise ExternalServiceError("The Sheets write session could not be closed.") from cause

    def _ensure_open(self) -> None:
        if self._closed:
            raise ConfigurationError("The Sheets writer is closed. Create a new instance.")

    def __enter__(self) -> Self:
        """Manage the lifecycle of owned sessions."""
        self._ensure_open()
        return self

    def __exit__(
        self,
        kind_error: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Release resources without hiding an earlier exception from the block."""
        try:
            self.close()
        except ExternalServiceError:
            if error is None:
                raise
            LOGGER.warning("Sheets could not be closed after an earlier failure.")


def create_writer_sheets(
    configuration: Config, credentials: Credentials, *, clock: Clock
) -> SheetsClientWriter:
    """Create owned write sessions with explicit scopes and no implicit login."""
    if configuration.spreadsheet_id is None:
        raise ConfigurationError("Configure the Google Sheets document identifier.")
    if not WRITE_SCOPES.intersection(configuration.google_scopes):
        raise ConfigurationError("Configure an explicit Sheets write scope.")
    scopes = cast(_ScopedCredentials, credentials)
    if not hasattr(credentials, "has_scopes") or not any(
        scopes.has_scopes([scope]) for scope in WRITE_SCOPES
    ):
        raise AuthorizationError("The credentials do not declare Sheets write permissions.")
    refresh = Session()
    authorized: Session | None = None
    try:
        authorized = cast(_SessionFactory, AuthorizedSession)(
            credentials,
            max_refresh_attempts=0,
            refresh_timeout=configuration.network_timeout_seconds,
            auth_request=CredentialsRequest(session=refresh),
        )
        return SheetsClientWriter(
            configuration.spreadsheet_id,
            session=authorized,
            backup_directory=configuration.backup_directory,
            clock=clock,
            timeout_seconds=configuration.network_timeout_seconds,
            attempts=configuration.max_attempts,
            wait_minimum_seconds=configuration.retry_min_wait_seconds,
            wait_maximum_seconds=configuration.retry_max_wait_seconds,
            close_session=True,
            session_refresh=refresh,
        )
    except Exception:
        try:
            if authorized is not None:
                authorized.close()
        finally:
            refresh.close()
        raise


def _validate_request(
    sheet: str, column: int, header: str, rows: frozenset[int], revision: str
) -> None:
    if sheet not in EXPECTED_SHEETS:
        raise StructureError("The requested sheet is not a client sheet.")
    if type(column) is not int or not 1 <= column <= MAXIMUM_COLUMN:
        raise StructureError("The weekly column must be a valid Sheets index.")
    if (
        not header.strip()
        or len(header.strip()) > 50000
        or header.strip().startswith("=")
        or any(ord(character) < 32 and character not in "\t\n\r" for character in header)
    ):
        raise StructureError("The weekly header must be valid literal text.")
    if any(type(row) is not int or row < 3 for row in rows):
        raise StructureError("Rows must be valid client indices.")
    if not revision.strip():
        raise StructureError("Writing requires a source revision.")


def _prepare(
    reader: SheetsClientReader,
    document: Object,
    name: str,
    column: int,
    header: str,
    selected: frozenset[int],
) -> list[Object]:
    destination = next(
        _object(element)
        for element in _list(document["sheets"])
        if _object(_object(element)["properties"])["title"] == name
    )
    properties = _object(destination["properties"])
    identifier = cast(int, properties["sheetId"])
    dimensions = _object(properties["gridProperties"])
    capacity = cast(int, dimensions["columnCount"])
    weeks = reader.read_headers(name)
    matching = [catalog_index for catalog_index, text in weeks if text.strip() == header.strip()]
    new = not matching
    next_column = weeks[-1][0] + 1 if weeks else (4 if name == "Balanz" else 3)
    if (not new and matching != [column]) or (new and column != next_column):
        raise StructureError("The column is not the existing or next weekly column.")
    clients = frozenset(client.row for client in reader.read_clients(name))
    if not selected <= clients:
        raise StructureError("The selection includes rows without clients in the current source.")
    affected = clients | ({2} if new else set())
    _validate_restrictions(destination, identifier, column, affected)
    cells = _cells(destination)
    for row in affected:
        current = cells.get((row, column), {})
        if current.get("dataValidation"):
            raise StructureError("The destination contains data validation.")
        if row != 2 or new:
            for field in ("userEnteredValue", "effectiveValue"):
                value = _object(current.get(field, {}))
                if value and (
                    set(value) != {"stringValue"}
                    or not isinstance(value["stringValue"], str)
                    or value["stringValue"].strip().casefold() not in {"", "x"}
                ):
                    raise StructureError("The week contains formulas or values unrelated to marks.")
    requests: list[Object] = []
    if column > capacity:
        requests.append(
            {
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": identifier,
                        "gridProperties": {"columnCount": column},
                    },
                    "fields": "gridProperties.columnCount",
                }
            }
        )
    if new:
        requests.append(
            {
                "updateCells": {
                    "start": {"sheetId": identifier, "rowIndex": 1, "columnIndex": column - 1},
                    "rows": [{"values": [{"userEnteredValue": {"stringValue": header.strip()}}]}],
                    "fields": "userEnteredValue",
                }
            }
        )
    for group in _group(clients):
        rows_json: list[Object] = []
        for row in group:
            color = (
                {"red": 118 / 255, "green": 146 / 255, "blue": 60 / 255}
                if row in selected
                else {"red": 1.0, "green": 1.0, "blue": 1.0}
            )
            rows_json.append(
                {"values": [{"userEnteredFormat": {"backgroundColorStyle": {"rgbColor": color}}}]}
            )
        requests.append(
            {
                "updateCells": {
                    "start": {
                        "sheetId": identifier,
                        "rowIndex": group[0] - 1,
                        "columnIndex": column - 1,
                    },
                    "rows": rows_json,
                    "fields": (
                        "userEnteredValue,userEnteredFormat.backgroundColor,userEnteredFormat.backgroundColorStyle"
                    ),
                }
            }
        )
    # Conservative local limit preserving the atomicity of a single batch.
    if len(json.dumps(requests, ensure_ascii=False).encode("utf-8")) > 1_900_000:
        raise StructureError(
            "The batch exceeds the local limit and requires a different writing mode."
        )
    return requests


def _validate_restrictions(
    sheet: Object, identifier: int, column: int, rows: frozenset[int]
) -> None:
    if sheet.get("tables"):
        raise StructureError("The writer requires a sheet without Sheets tables.")
    ranges = list(_list(sheet.get("merges", [])))
    for field in ("protectedRanges", "bandedRanges"):
        ranges.extend(_object(element).get("range", {}) for element in _list(sheet.get(field, [])))
    for rule in _list(sheet.get("conditionalFormats", [])):
        ranges.extend(_list(_object(rule).get("ranges", [{}])) or [{}])
    if any(_intersects(_object(period), identifier, column, rows) for period in ranges):
        raise StructureError(
            "The week intersects automatic formatting, protection, or merged cells."
        )


def _intersects(period: Object, identifier: int, column: int, rows: frozenset[int]) -> bool:
    if period.get("sheetId", identifier) != identifier:
        raise StructureError("A sheet range has an incompatible reference.")
    indices: dict[str, int] = {}
    for field in ("startRowIndex", "endRowIndex", "startColumnIndex", "endColumnIndex"):
        value = period.get(field)
        if value is not None:
            if type(value) is not int or value < 0:
                raise StructureError("A range has incompatible indices.")
            indices[field] = value
    for axis in ("Row", "Column"):
        if indices.get(f"end{axis}Index", 2**63) <= indices.get(f"start{axis}Index", 0):
            raise StructureError("A range has incompatible bounds.")
    return indices.get("startColumnIndex", 0) <= column - 1 < indices.get(
        "endColumnIndex", 2**63
    ) and any(
        indices.get("startRowIndex", 0) <= row - 1 < indices.get("endRowIndex", 2**63)
        for row in rows
    )


def _cells(sheet: Object) -> dict[tuple[int, int], Object]:
    result: dict[tuple[int, int], Object] = {}
    for block in _list(sheet.get("data", [])):
        grid = _object(block)
        start_row = cast(int, grid.get("startRow", 0))
        start_column = cast(int, grid.get("startColumn", 0))
        for offset_row, row in enumerate(_list(grid.get("rowData", [])), 1):
            for offset_column, cell in enumerate(_list(_object(row).get("values", [])), 1):
                content = _object(cell)
                if any(
                    field in content
                    for field in ("pivotTable", "dataSourceTable", "dataSourceFormula")
                ):
                    raise StructureError(
                        "The writer requires a sheet without pivots or data sources."
                    )
                result[(start_row + offset_row, start_column + offset_column)] = content
    return result


def _group(rows: frozenset[int]) -> list[list[int]]:
    groups: list[list[int]] = []
    for row in sorted(rows):
        if not groups or row != groups[-1][-1] + 1:
            groups.append([])
        groups[-1].append(row)
    return groups


def _object(value: object) -> Object:
    if not isinstance(value, dict):
        raise StructureError("Sheets returned an object incompatible with writing.")
    return cast(Object, value)


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise StructureError("Sheets returned a collection incompatible with writing.")
    return cast(list[object], value)
