"""Document validation and destination preservation after disk failures."""

import logging
from pathlib import Path
from typing import cast
from unittest.mock import call

import pytest
from pytest_mock import MockerFixture

from client_automation.core.errors import (
    DomainValidationError,
    PersistenceError,
    StructureError,
)
from client_automation.infra.json_storage import (
    JsonValue,
    read_json,
    save_json,
)


@pytest.mark.parametrize(
    "document",
    [
        None,
        True,
        False,
        0,
        -7,
        12.5,
        "R\u00e1dono \u2014 \u5ba2\u6237",
        [],
        {},
        {"clients": [None, True, 42, 1.5, "M\u00edzafe", {"active": False}]},
    ],
)
def test_saved_and_read_preserve_values_json(tmp_path: Path, document: JsonValue) -> None:
    path = tmp_path / "new" / "folder" / "document.json"

    save_json(path, document)

    assert read_json(path) == document
    assert list(path.parent.iterdir()) == [path]


def test_replaces_file_and_generates_utf8_reproducible(tmp_path: Path) -> None:
    path = tmp_path / "document.json"
    path.write_bytes(b"contenido anterior")

    save_json(path, {"zeta": "M\u00edzafe", "alpha": 1})

    assert path.read_bytes() == '{\n  "alpha": 1,\n  "zeta": "M\u00edzafe"\n}\n'.encode()


def test_accepts_bom_of_windows(tmp_path: Path) -> None:
    path = tmp_path / "document.json"
    path.write_text('{"name": "R\u00e1dono"}', encoding="utf-8-sig")

    assert read_json(path) == {"name": "R\u00e1dono"}


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"{incomplete",
        b"{} {}",
        b'{"clave": 1, "clave": 2}',
        b'{"anidado": [{"clave": 1, "clave": 2}]}',
        b"NaN",
        b"Infinity",
        b"-Infinity",
        b'{"numero": 1e9999}',
        b"[" * 2000 + b"0" + b"]" * 2000,
        b'"\xff"',
    ],
)
def test_rejects_documents_invalid_without_modify(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "document.json"
    path.write_bytes(content)

    with pytest.raises(StructureError) as captured:
        read_json(path)

    assert captured.value.__cause__ is not None
    assert path.read_bytes() == content


def test_file_missing_is_error_and_no_equals_to_null(tmp_path: Path) -> None:
    path = tmp_path / "missing.json"

    with pytest.raises(PersistenceError) as captured:
        read_json(path)

    assert isinstance(captured.value.__cause__, FileNotFoundError)
    assert not path.exists()


def test_translates_error_of_permissions_of_read(tmp_path: Path, mocker: MockerFixture) -> None:
    cause = PermissionError("private technical detail")
    mocker.patch.object(Path, "read_text", side_effect=cause)

    with pytest.raises(PersistenceError) as captured:
        read_json(tmp_path / "document.json")

    assert captured.value.__cause__ is cause
    assert "private" not in str(captured.value)


@pytest.mark.parametrize(
    "document",
    [float("nan"), float("inf"), -float("inf"), {1: "numeric key"}, (1, 2), {1}, b"bytes"],
)
def test_rejects_kinds_no_json_before_of_create_directories(
    tmp_path: Path, document: object
) -> None:
    path = tmp_path / "new" / "document.json"

    with pytest.raises(DomainValidationError):
        save_json(path, cast(JsonValue, document))

    assert not path.parent.exists()


def test_lifecycle_no_modifies_destination(tmp_path: Path) -> None:
    path = tmp_path / "document.json"
    path.write_bytes(b"anterior")
    document: list[JsonValue] = []
    document.append(document)

    with pytest.raises(DomainValidationError) as captured:
        save_json(path, document)

    assert isinstance(captured.value.__cause__, RecursionError)
    assert path.read_bytes() == b"anterior"
    assert list(tmp_path.iterdir()) == [path]


def test_unicode_no_encodable_no_modifies_destination(tmp_path: Path) -> None:
    path = tmp_path / "document.json"
    path.write_bytes(b"anterior")

    with pytest.raises(DomainValidationError) as captured:
        save_json(path, {"name": "\ud800"})

    assert isinstance(captured.value.__cause__, UnicodeEncodeError)
    assert path.read_bytes() == b"anterior"


@pytest.mark.parametrize(
    "operation",
    [
        "pathlib.Path.mkdir",
        "client_automation.infra.json_storage.NamedTemporaryFile",
        "client_automation.infra.json_storage.fsync",
        "pathlib.Path.replace",
    ],
)
@pytest.mark.parametrize("destination_previous", [True, False])
def test_failure_of_saved_preserves_destination_and_cleans_temporary(
    tmp_path: Path, mocker: MockerFixture, operation: str, destination_previous: bool
) -> None:
    path = tmp_path / "document.json"
    if destination_previous:
        path.write_bytes(b"anterior")
    cause = OSError("disk no available")
    mocker.patch(operation, side_effect=cause)

    with pytest.raises(PersistenceError) as captured:
        save_json(path, {"new": True})

    assert captured.value.__cause__ is cause
    if destination_previous:
        assert path.read_bytes() == b"anterior"
        assert list(tmp_path.iterdir()) == [path]
    else:
        assert list(tmp_path.iterdir()) == []


def test_failure_of_cleanup_preserves_cause_main_and_warns_without_data_private(
    tmp_path: Path, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "client-private.json"
    path.write_bytes(b"anterior")
    cause = PermissionError("destination locked")
    mocker.patch.object(Path, "replace", side_effect=cause)
    mocker.patch.object(Path, "unlink", side_effect=PermissionError("path private"))

    with caplog.at_level(logging.WARNING), pytest.raises(PersistenceError) as captured:
        save_json(path, {"name": "Name private"})

    assert captured.value.__cause__ is cause
    assert path.read_bytes() == b"anterior"
    assert len(caplog.records) == 1
    assert "temporary JSON" in caplog.text
    assert "private" not in caplog.text
    assert caplog.records[0].exc_info is None


def test_records_of_success_no_include_paths_nor_clients(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "client-private.json"

    with caplog.at_level(logging.DEBUG):
        save_json(path, {"name": "Name private"})
        read_json(path)

    assert len(caplog.records) == 2
    assert "private" not in caplog.text


class FakeWindowsError(OSError):
    def __init__(self, code: int) -> None:
        super().__init__("Detail private of system operational")
        self.winerror = code


@pytest.mark.parametrize("code", [32, 33])
def test_lock_temporary_retries_only_replacement_and_saves_without_expose_data(
    tmp_path: Path, mocker: MockerFixture, caplog: pytest.LogCaptureFixture, code: int
) -> None:
    path = tmp_path / "client-private.json"
    path.write_bytes(b"anterior")
    replace_original = Path.replace
    attempts = 0

    def replace_with_lock(source: Path, destination: Path) -> Path:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            assert path.read_bytes() == b"anterior"
            raise FakeWindowsError(code)
        return replace_original(source, destination)

    mocker.patch.object(Path, "replace", autospec=True, side_effect=replace_with_lock)
    waits = mocker.patch("tenacity.nap.time.sleep")

    with caplog.at_level(logging.WARNING):
        save_json(path, {"name": "Name private"})

    assert attempts == 3
    assert waits.call_args_list == [call(0.05), call(0.1)]
    assert read_json(path) == {"name": "Name private"}
    assert list(tmp_path.iterdir()) == [path]
    assert len(caplog.records) == 2
    assert "private" not in caplog.text
    assert all(registration.exc_info is None for registration in caplog.records)


@pytest.mark.parametrize("code", [32, 33])
def test_lock_persistent_exhausts_attempts_and_preserves_destination(
    tmp_path: Path, mocker: MockerFixture, caplog: pytest.LogCaptureFixture, code: int
) -> None:
    path = tmp_path / "document.json"
    path.write_bytes(b"anterior")
    cause = FakeWindowsError(code)
    replacement = mocker.patch.object(Path, "replace", side_effect=cause)
    waits = mocker.patch("tenacity.nap.time.sleep")

    with caplog.at_level(logging.WARNING), pytest.raises(PersistenceError) as captured:
        save_json(path, {"new": True})

    assert captured.value.__cause__ is cause
    assert replacement.call_count == 4
    assert waits.call_args_list == [call(0.05), call(0.1), call(0.2)]
    assert path.read_bytes() == b"anterior"
    assert list(tmp_path.iterdir()) == [path]
    assert len(caplog.records) == 3


@pytest.mark.parametrize("code", [5, 2, 112])
def test_error_permanent_of_replacement_no_retries(
    tmp_path: Path, mocker: MockerFixture, code: int
) -> None:
    path = tmp_path / "document.json"
    path.write_bytes(b"anterior")
    replacement = mocker.patch.object(Path, "replace", side_effect=FakeWindowsError(code))
    waits = mocker.patch("tenacity.nap.time.sleep")

    with pytest.raises(PersistenceError):
        save_json(path, {"new": True})

    replacement.assert_called_once()
    waits.assert_not_called()
    assert path.read_bytes() == b"anterior"
