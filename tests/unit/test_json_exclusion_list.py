"""Normalization, legacy compatibility, and immediate exclusion persistence."""

import logging
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from client_automation.core.contracts import ExclusionListPort
from client_automation.core.errors import (
    ConcurrencyError,
    DomainValidationError,
    PersistenceError,
    StructureError,
)
from client_automation.infra.json_exclusion_list import JsonExclusionList
from client_automation.infra.json_storage import JsonValue, read_json, save_json


def test_file_missing_starts_list_empty_without_create_folders(tmp_path: Path) -> None:
    path = tmp_path / "new" / "exclusions.json"
    require_list: ExclusionListPort = JsonExclusionList(path)

    assert not require_list.contains("The Counter")
    assert not path.parent.exists()


def test_load_format_legacy_without_modify_original(tmp_path: Path) -> None:
    path = tmp_path / "exclusions.json"
    save_json(path, ["  K\u00daKEVE, R\u00e1dono  ", "The Counter", "Provider (IOL)"])
    content_previous = path.read_bytes()
    require_list: ExclusionListPort = JsonExclusionList(path)

    assert require_list.contains("kukeve radono")
    assert require_list.contains(" THE   COUNTER ")
    assert require_list.contains("provider iol")
    assert not require_list.contains("Radono Kukeve")
    assert not require_list.contains("Provider")
    assert not require_list.contains("The Counterlet")
    assert path.read_bytes() == content_previous


def test_query_uses_snapshot_without_access_on_disk(tmp_path: Path, mocker: MockerFixture) -> None:
    path = tmp_path / "exclusions.json"
    save_json(path, ["The Counter"])
    require_list = JsonExclusionList(path)
    read = mocker.patch.object(Path, "read_text", side_effect=PermissionError("locked"))

    assert require_list.contains("The Counter")
    assert not require_list.contains("Other contact")
    read.assert_not_called()


def test_add_saves_immediately_and_preserves_the_name_original(tmp_path: Path) -> None:
    path = tmp_path / "new" / "exclusions.json"
    require_list: ExclusionListPort = JsonExclusionList(path)
    name = "  K\u00daKEVE, R\u00e1dono  "

    require_list.add(name)

    assert require_list.contains("kukeve radono")
    assert read_json(path) == [name]
    assert JsonExclusionList(path).contains("kukeve radono")
    assert list(path.parent.iterdir()) == [path]


def test_changes_successive_preserve_inputs_and_save_in_order(tmp_path: Path) -> None:
    path = tmp_path / "exclusions.json"
    require_list = JsonExclusionList(path)

    require_list.add("Provider Z")
    require_list.add("Counter A")
    require_list.add("Family")

    assert read_json(path) == ["Counter A", "Family", "Provider Z"]
    assert require_list.contains("provider z")
    assert require_list.contains("counter a")
    assert require_list.contains("family")


def test_name_equivalent_is_idempotent_and_no_again_to_write(
    tmp_path: Path, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "exclusions.json"
    require_list = JsonExclusionList(path)
    require_list.add("  K\u00daKEVE, R\u00e1dono  ")
    content_previous = path.read_bytes()
    write = mocker.patch("client_automation.infra.json_exclusion_list.save_json")

    with caplog.at_level(logging.INFO):
        require_list.add("kukeve radono")
        require_list.add("K\u00daKEVE, R\u00c1DONO")

    write.assert_not_called()
    assert path.read_bytes() == content_previous
    assert not caplog.records


def test_duplicates_legacy_preserve_without_add_equivalent(tmp_path: Path) -> None:
    path = tmp_path / "exclusions.json"
    original = ["R\u00e1dono K\u00fakeve", "radono kukeve", "R\u00e1dono K\u00fakeve"]
    document: list[JsonValue] = list(original)
    save_json(path, document)
    require_list = JsonExclusionList(path)

    require_list.add("R\u00c1DONO K\u00daKEVE")
    assert read_json(path) == original
    require_list.add("The Counter")

    assert read_json(path) == sorted([*original, "The Counter"])
    assert require_list.contains("radono kukeve")


@pytest.mark.parametrize("name", ["", "  \t\n", "!!!", "--- ()", "\u0301"])
def test_name_without_text_meaningful_rejects_before_of_read_or_write(
    tmp_path: Path, name: str, mocker: MockerFixture
) -> None:
    path = tmp_path / "new" / "exclusions.json"
    require_list = JsonExclusionList(path)
    read = mocker.patch.object(Path, "read_text", side_effect=PermissionError("locked"))

    with pytest.raises(DomainValidationError, match="meaningful text"):
        require_list.add(name)

    assert not require_list.contains(name)
    assert not path.parent.exists()
    read.assert_not_called()


@pytest.mark.parametrize("name", ["", "  ", "!!!", "\u0301"])
def test_query_without_text_meaningful_returns_false(tmp_path: Path, name: str) -> None:
    assert not JsonExclusionList(tmp_path / "exclusions.json").contains(name)


@pytest.mark.parametrize("document", [None, {}, "Name", 1, [None], [False], [42], [["Name"]]])
def test_schema_incorrect_no_interprets_as_list_empty(tmp_path: Path, document: JsonValue) -> None:
    path = tmp_path / "exclusions.json"
    save_json(path, document)
    content_previous = path.read_bytes()

    with pytest.raises(StructureError):
        JsonExclusionList(path)

    assert path.read_bytes() == content_previous


@pytest.mark.parametrize("name", ["", "  ", "!!!"])
def test_input_persisted_without_text_meaningful_rejects(tmp_path: Path, name: str) -> None:
    path = tmp_path / "exclusions.json"
    save_json(path, ["Valid name", name])
    content_previous = path.read_bytes()

    with pytest.raises(DomainValidationError):
        JsonExclusionList(path)

    assert path.read_bytes() == content_previous


def test_json_corrupt_no_interprets_as_list_empty(tmp_path: Path) -> None:
    path = tmp_path / "exclusions.json"
    path.write_bytes(b"[incomplete")

    with pytest.raises(StructureError):
        JsonExclusionList(path)

    assert path.read_bytes() == b"[incomplete"


def test_error_of_access_on_load_no_confuses_with_absence(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    cause = PermissionError("detail private")
    mocker.patch.object(Path, "read_text", side_effect=cause)

    with pytest.raises(PersistenceError) as captured:
        JsonExclusionList(tmp_path / "exclusions.json")

    assert captured.value.__cause__ is cause
    assert "private" not in str(captured.value)


@pytest.mark.parametrize("previous_file", [True, False])
@pytest.mark.parametrize(
    "operation",
    ["client_automation.infra.json_storage.fsync", "pathlib.Path.replace"],
)
def test_failure_of_saved_no_leaves_exclusion_only_in_memory_and_allows_retry(
    tmp_path: Path, mocker: MockerFixture, previous_file: bool, operation: str
) -> None:
    path = tmp_path / "exclusions.json"
    if previous_file:
        save_json(path, ["Previous"])
    require_list = JsonExclusionList(path)
    cause = OSError("file locked")
    failure = mocker.patch(operation, side_effect=cause)

    with pytest.raises(PersistenceError) as captured:
        require_list.add("New")

    assert captured.value.__cause__ is cause
    assert not require_list.contains("New")
    if previous_file:
        assert require_list.contains("Previous")
        assert read_json(path) == ["Previous"]
        assert list(tmp_path.iterdir()) == [path]
    else:
        assert list(tmp_path.iterdir()) == []
    mocker.stop(failure)
    require_list.add("New")

    assert require_list.contains("New")
    assert JsonExclusionList(path).contains("New")


def test_error_on_check_file_no_modifies_state(tmp_path: Path, mocker: MockerFixture) -> None:
    path = tmp_path / "exclusions.json"
    require_list = JsonExclusionList(path)
    read = mocker.patch.object(Path, "read_text", side_effect=PermissionError("locked"))

    with pytest.raises(PersistenceError):
        require_list.add("New")

    assert not require_list.contains("New")
    assert not path.exists()
    mocker.stop(read)
    require_list.add("New")
    assert require_list.contains("New")


def test_text_no_encodable_no_saves_nor_includes_to_the_session(tmp_path: Path) -> None:
    path = tmp_path / "exclusions.json"
    require_list = JsonExclusionList(path)

    with pytest.raises(DomainValidationError):
        require_list.add("Axa\ud800")

    assert not require_list.contains("Axa")
    assert not path.exists()


@pytest.mark.parametrize("change", ["creation", "modification", "removal"])
def test_change_external_no_overwrites_nor_modifies_the_snapshot(
    tmp_path: Path, change: str
) -> None:
    path = tmp_path / "exclusions.json"
    if change != "creation":
        save_json(path, ["Previous"])
    require_list = JsonExclusionList(path)
    if change == "removal":
        path.unlink()
        content_external = None
    else:
        save_json(path, ["External"])
        content_external = path.read_bytes()

    with pytest.raises(ConcurrencyError):
        require_list.add("New")

    assert not require_list.contains("New")
    assert not require_list.contains("External")
    assert require_list.contains("Previous") is (change != "creation")
    if content_external is None:
        assert not path.exists()
    else:
        assert path.read_bytes() == content_external


def test_repeat_exclusion_after_removal_external_also_detects_conflict(
    tmp_path: Path,
) -> None:
    path = tmp_path / "exclusions.json"
    save_json(path, ["Previous"])
    require_list = JsonExclusionList(path)
    path.unlink()

    with pytest.raises(ConcurrencyError):
        require_list.add("PREVIOUS")

    assert require_list.contains("Previous")
    assert not path.exists()


def test_corruption_later_to_load_prevents_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "exclusions.json"
    save_json(path, ["Previous"])
    require_list = JsonExclusionList(path)
    path.write_bytes(b"[incomplete")

    with pytest.raises(StructureError):
        require_list.add("New")

    assert require_list.contains("Previous")
    assert not require_list.contains("New")
    assert path.read_bytes() == b"[incomplete"


def test_two_sessions_no_lose_the_exclusion_that_saved_the_first(tmp_path: Path) -> None:
    path = tmp_path / "exclusions.json"
    first = JsonExclusionList(path)
    second = JsonExclusionList(path)
    first.add("First")

    with pytest.raises(ConcurrencyError):
        second.add("Second")

    assert read_json(path) == ["First"]
    assert not second.contains("Second")
    new_session = JsonExclusionList(path)
    new_session.add("Second")
    assert read_json(path) == ["First", "Second"]


def test_change_only_of_format_allows_add(tmp_path: Path) -> None:
    path = tmp_path / "exclusions.json"
    save_json(path, ["Previous"])
    require_list = JsonExclusionList(path)
    path.write_text('["Previous"]', encoding="utf-8-sig")

    require_list.add("New")

    assert read_json(path) == ["New", "Previous"]


def test_log_of_success_no_includes_names_nor_paths(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "require_list-private.json"
    require_list = JsonExclusionList(path)

    with caplog.at_level(logging.INFO):
        require_list.add("Name private")

    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.INFO
    assert "saved" in caplog.text
    assert "private" not in caplog.text
    assert caplog.records[0].exc_info is None
