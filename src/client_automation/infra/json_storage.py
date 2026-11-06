"""Strict JSON reading and atomic replacement of local documents."""

import json
import logging
from math import isfinite
from os import fsync
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import cast

from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from client_automation.core.errors import (
    DomainValidationError,
    PersistenceError,
    StructureError,
)

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None

LOGGER = logging.getLogger(__name__)


def read_json(path: Path) -> JsonValue:
    """Read a UTF-8 document and require valid JSON."""
    try:
        content = path.read_text(encoding="utf-8-sig")
    except UnicodeError as cause:
        raise StructureError("The JSON document is not UTF-8 encoded.") from cause
    except OSError as cause:
        raise PersistenceError("The JSON document could not be read.") from cause

    try:
        document: object = json.loads(content, object_pairs_hook=_object_without_duplicates)
        _validate_value(document)
    except (ValueError, RecursionError) as cause:
        raise StructureError("The document does not contain valid JSON.") from cause

    LOGGER.debug("JSON document read and validated.")
    return cast(JsonValue, document)


def save_json(path: Path, document: JsonValue) -> None:
    """Write through a temporary file and replace the destination on completion."""
    try:
        _validate_value(document)
        content = json.dumps(
            document, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
        )
        content_utf8 = (content + "\n").encode("utf-8")
    except (ValueError, RecursionError) as cause:
        raise DomainValidationError("The document cannot be represented as UTF-8 JSON.") from cause

    path_temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=".json-", suffix=".tmp", delete=False
        ) as temporary_file:
            path_temporary = Path(temporary_file.name)
            temporary_file.write(content_utf8)
            temporary_file.flush()
            fsync(temporary_file.fileno())
        _replace_temporary(path_temporary, path)
        path_temporary = None
    except OSError as cause:
        raise PersistenceError("The JSON document could not be saved.") from cause
    finally:
        if path_temporary is not None:
            _remove_temporary(path_temporary)

    LOGGER.debug("JSON document saved through atomic replacement.")


def _object_without_duplicates(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("The JSON object contains duplicate keys.")
        result[key] = value
    return result


def _validate_value(value: object) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError("JSON requires finite numbers.")
        return
    if isinstance(value, list):
        for element in cast(list[object], value):
            _validate_value(element)
        return
    if isinstance(value, dict):
        for key, element in cast(dict[object, object], value).items():
            if not isinstance(key, str):
                raise ValueError("JSON keys must be strings.")
            _validate_value(element)
        return
    raise ValueError("The document contains a type unsupported by JSON.")


def _remove_temporary(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        LOGGER.warning("A temporary JSON file could not be removed after a save failure.")


def _replace_temporary(path_temporary: Path, path_destination: Path) -> None:
    for attempt in Retrying(
        retry=retry_if_exception(_is_lock_temporary),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=0.05, min=0.05, max=0.2),
        before_sleep=_warn_retry,
        reraise=True,
    ):
        with attempt:
            path_temporary.replace(path_destination)


def _is_lock_temporary(cause: BaseException) -> bool:
    return isinstance(cause, OSError) and getattr(cause, "winerror", None) in {32, 33}


def _warn_retry(state: RetryCallState) -> None:
    LOGGER.warning(
        "JSON replacement temporarily locked; retrying after attempt %s.",
        state.attempt_number,
    )
