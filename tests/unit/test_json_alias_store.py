"""Alias compatibility, conflicts, and explicit session persistence."""

import logging
from dataclasses import replace
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from client_automation.core.contracts import AliasStorePort
from client_automation.core.errors import (
    ConcurrencyError,
    DomainValidationError,
    PersistenceError,
    StructureError,
)
from client_automation.core.models import AliasDestination, ContactAlias
from client_automation.infra.json_alias_store import JsonAliasStore
from client_automation.infra.json_storage import JsonValue, read_json, save_json


@pytest.fixture
def alias() -> ContactAlias:
    return ContactAlias(
        raw_name="  Contact private (IOL)  ",
        phone="+5491100000000",
        people=(AliasDestination(person_id="p0001", sheets=frozenset({"IOL", "Bull"})),),
    )


@pytest.fixture
def input() -> dict[str, JsonValue]:
    return {
        "people": [{"person_id": "p0001", "sheets": ["IOL", "Bull"]}],
        "phone": "+5491100000000",
    }


def test_absence_starts_session_empty_without_create_directories(tmp_path: Path) -> None:
    path = tmp_path / "new" / "aliases.json"
    store: AliasStorePort = JsonAliasStore(path)

    assert store.find_by_name("nonexistent") is None
    assert store.find_by_phone("+5491100000000") == ()
    assert not path.parent.exists()


def test_reads_format_legacy_and_preserves_name_exact(
    tmp_path: Path, alias: ContactAlias, input: dict[str, JsonValue]
) -> None:
    path = tmp_path / "aliases.json"
    save_json(path, {alias.raw_name: input})
    content_previous = path.read_bytes()

    store: AliasStorePort = JsonAliasStore(path)

    assert store.find_by_name(alias.raw_name) == alias
    assert store.find_by_name(alias.raw_name.strip()) is None
    assert store.find_by_name(alias.raw_name.lower()) is None
    assert store.find_by_phone("+5491100000000") == (alias,)
    assert store.find_by_phone("5491100000000") == ()
    assert store.find_by_phone("") == ()
    assert path.read_bytes() == content_previous


def test_accepts_input_legacy_without_phone(
    tmp_path: Path, alias: ContactAlias, input: dict[str, JsonValue]
) -> None:
    path = tmp_path / "aliases.json"
    del input["phone"]
    save_json(path, {alias.raw_name: input})

    store = JsonAliasStore(path)

    assert store.find_by_name(alias.raw_name) == replace(alias, phone=None)


def test_register_only_modifies_memory_and_save_preserves_format(
    tmp_path: Path, alias: ContactAlias
) -> None:
    path = tmp_path / "new" / "aliases.json"
    store: AliasStorePort = JsonAliasStore(path)
    holder = AliasDestination(person_id="p0002", sheets=frozenset({"Balanz"}))
    alias = replace(alias, people=(*alias.people, holder))

    assert store.register(alias) is None
    assert store.find_by_name(alias.raw_name) == alias
    assert not path.parent.exists()

    store.save()

    assert read_json(path) == {
        alias.raw_name: {
            "people": [
                {"person_id": "p0001", "sheets": ["Bull", "IOL"]},
                {"person_id": "p0002", "sheets": ["Balanz"]},
            ],
            "phone": "+5491100000000",
        }
    }
    assert JsonAliasStore(path).find_by_name(alias.raw_name) == alias
    assert list(path.parent.iterdir()) == [path]


def test_save_session_empty_creates_object_json(tmp_path: Path) -> None:
    path = tmp_path / "aliases.json"

    JsonAliasStore(path).save()

    assert read_json(path) == {}


def test_alias_without_name_preserves_by_phone(tmp_path: Path, alias: ContactAlias) -> None:
    path = tmp_path / "aliases.json"
    alias = replace(alias, raw_name="")
    store = JsonAliasStore(path)
    store.register(alias)
    store.save()

    assert JsonAliasStore(path).find_by_phone(alias.phone or "") == (alias,)


def test_phone_shared_returns_all_the_destinations_in_order_stable(
    tmp_path: Path, alias: ContactAlias
) -> None:
    path = tmp_path / "aliases.json"
    first = replace(alias, raw_name="Alias A")
    second = replace(
        alias,
        raw_name="Alias B",
        people=(AliasDestination(person_id="p0002", sheets=frozenset({"PPI"})),),
    )
    store = JsonAliasStore(path)
    store.register(second)
    store.register(first)
    store.register(replace(alias, raw_name="Other", phone=None))

    assert store.find_by_phone("+5491100000000") == (first, second)
    store.save()
    assert JsonAliasStore(path).find_by_phone("+5491100000000") == (first, second)


def test_records_compatible_are_idempotent_although_changes_the_order(
    tmp_path: Path, alias: ContactAlias, caplog: pytest.LogCaptureFixture
) -> None:
    holder = AliasDestination(person_id="p0002", sheets=frozenset({"PPI"}))
    alias = replace(alias, people=(*alias.people, holder))
    store = JsonAliasStore(tmp_path / "aliases.json")

    with caplog.at_level(logging.WARNING):
        assert store.register(alias) is None
        assert store.register(replace(alias, people=tuple(reversed(alias.people)))) is None
        assert store.register(replace(alias, phone=None)) is None
        assert store.register(alias) is None

    assert store.find_by_name(alias.raw_name) == alias
    assert store.find_by_phone("+5491100000000") == (alias,)
    assert not caplog.records


def test_complete_phone_no_modifies_destinations_and_persists(
    tmp_path: Path, alias: ContactAlias
) -> None:
    path = tmp_path / "aliases.json"
    store = JsonAliasStore(path)
    store.register(replace(alias, phone=None))
    store.save()

    assert store.register(alias) is None
    assert store.find_by_phone("+5491100000000") == (alias,)
    assert JsonAliasStore(path).find_by_phone("+5491100000000") == ()
    store.save()

    assert JsonAliasStore(path).find_by_name(alias.raw_name) == alias


@pytest.mark.parametrize("conflict", ["person", "sheets", "phone"])
def test_conflict_preserves_all_the_input_and_no_exposes_data_personal(
    tmp_path: Path,
    alias: ContactAlias,
    conflict: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "aliases.json"
    store = JsonAliasStore(path)
    store.register(alias)
    store.save()
    content_previous = path.read_bytes()
    if conflict == "person":
        new = replace(
            alias, people=(AliasDestination(person_id="p0002", sheets=frozenset({"IOL"})),)
        )
    elif conflict == "sheets":
        new = replace(alias, people=(replace(alias.people[0], sheets=frozenset({"PPI"})),))
    else:
        new = replace(alias, phone="+5491199999999")

    with caplog.at_level(logging.WARNING):
        warning = store.register(new)

    assert warning is not None
    assert "preserved" in warning
    assert store.find_by_name(alias.raw_name) == alias
    assert store.find_by_phone("+5491100000000") == (alias,)
    assert store.find_by_phone("+5491199999999") == ()
    assert len(caplog.records) == 1
    for record_private in (alias.raw_name, "+5491100000000", "+5491199999999", "p0001", "p0002"):
        assert record_private not in warning
        assert record_private not in caplog.text
    store.save()
    assert path.read_bytes() == content_previous


@pytest.mark.parametrize(
    "document",
    [
        None,
        [],
        "require_object expected",
        {"Name": None},
        {"Name": {}},
        {"Name": {"people": [], "field_unknown": True}},
        {"Name": {"people": "require_list expected"}},
        {"Name": {"people": [], "phone": 123}},
        {"Name": {"people": [None]}},
        {"Name": {"people": [{"person_id": "p0001"}]}},
        {"Name": {"people": [{"person_id": "p0001", "sheets": ["IOL"], "row": 3}]}},
        {"Name": {"people": [{"person_id": 1, "sheets": ["IOL"]}]}},
        {"Name": {"people": [{"person_id": "p0001", "sheets": "IOL"}]}},
        {"Name": {"people": [{"person_id": "p0001", "sheets": [42]}]}},
        {"Name": {"people": [{"person_id": "p0001", "sheets": ["IOL", "IOL"]}]}},
    ],
)
def test_rejects_schema_incorrect_without_rewrite(tmp_path: Path, document: JsonValue) -> None:
    path = tmp_path / "aliases.json"
    save_json(path, document)
    content_previous = path.read_bytes()

    with pytest.raises(StructureError):
        JsonAliasStore(path)

    assert path.read_bytes() == content_previous


@pytest.mark.parametrize(
    "document",
    [
        {"Name": {"people": []}},
        {" ": {"people": [{"person_id": "p0001", "sheets": ["IOL"]}]}},
        {"Name": {"people": [{"person_id": " ", "sheets": ["IOL"]}]}},
        {"Name": {"people": [{"person_id": "p0001", "sheets": []}]}},
        {"Name": {"people": [{"person_id": "p0001", "sheets": [" "]}]}},
        {
            "Name": {
                "people": [{"person_id": "p0001", "sheets": ["IOL"]}],
                "phone": " ",
            }
        },
        {
            "Name": {
                "people": [
                    {"person_id": "p0001", "sheets": ["IOL"]},
                    {"person_id": "p0001", "sheets": ["Bull"]},
                ]
            }
        },
    ],
)
def test_rejects_identities_invalid_without_rewrite(tmp_path: Path, document: JsonValue) -> None:
    path = tmp_path / "aliases.json"
    save_json(path, document)
    content_previous = path.read_bytes()

    with pytest.raises(DomainValidationError):
        JsonAliasStore(path)

    assert path.read_bytes() == content_previous


def test_json_corrupt_no_starts_store_empty(tmp_path: Path) -> None:
    path = tmp_path / "aliases.json"
    path.write_bytes(b"{incomplete")

    with pytest.raises(StructureError):
        JsonAliasStore(path)

    assert path.read_bytes() == b"{incomplete"


def test_failure_of_access_no_confuses_with_file_missing(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    cause = PermissionError("access denied")
    mocker.patch.object(Path, "read_text", side_effect=cause)

    with pytest.raises(PersistenceError) as captured:
        JsonAliasStore(tmp_path / "aliases.json")

    assert captured.value.__cause__ is cause


@pytest.mark.parametrize("previous_file", [True, False])
def test_failure_of_saved_preserves_changes_for_retry_explicit(
    tmp_path: Path, alias: ContactAlias, mocker: MockerFixture, previous_file: bool
) -> None:
    path = tmp_path / "aliases.json"
    if previous_file:
        save_json(path, {})
    store = JsonAliasStore(path)
    store.register(alias)
    replacement = mocker.patch.object(Path, "replace", side_effect=PermissionError("locked"))

    with pytest.raises(PersistenceError):
        store.save()

    assert store.find_by_name(alias.raw_name) == alias
    if previous_file:
        assert read_json(path) == {}
    else:
        assert not path.exists()
    mocker.stop(replacement)
    store.save()
    assert JsonAliasStore(path).find_by_name(alias.raw_name) == alias


@pytest.mark.parametrize("change_external", ["creation", "modification", "removal"])
def test_detects_changes_external_without_overwrite_nor_discard_the_session(
    tmp_path: Path, alias: ContactAlias, change_external: str
) -> None:
    path = tmp_path / "aliases.json"
    if change_external != "creation":
        save_json(path, {})
    store = JsonAliasStore(path)
    store.register(alias)
    if change_external == "removal":
        path.unlink()
        content_external = None
    else:
        save_json(
            path,
            {"Alias external": {"people": [{"person_id": "p0002", "sheets": ["PPI"]}]}},
        )
        content_external = path.read_bytes()

    with pytest.raises(ConcurrencyError):
        store.save()

    assert store.find_by_name(alias.raw_name) == alias
    if content_external is None:
        assert not path.exists()
    else:
        assert path.read_bytes() == content_external


def test_two_sessions_no_lose_the_alias_that_saved_the_first(
    tmp_path: Path, alias: ContactAlias
) -> None:
    path = tmp_path / "aliases.json"
    first = JsonAliasStore(path)
    second = JsonAliasStore(path)
    first.register(alias)
    second.register(replace(alias, raw_name="Other contact"))
    first.save()

    with pytest.raises(ConcurrencyError):
        second.save()

    assert JsonAliasStore(path).find_by_name(alias.raw_name) == alias
    assert JsonAliasStore(path).find_by_name("Other contact") is None


def test_corruption_after_of_load_no_overwrites(tmp_path: Path, alias: ContactAlias) -> None:
    path = tmp_path / "aliases.json"
    save_json(path, {})
    store = JsonAliasStore(path)
    store.register(alias)
    path.write_bytes(b"{incomplete")

    with pytest.raises(StructureError):
        store.save()

    assert path.read_bytes() == b"{incomplete"
    assert store.find_by_name(alias.raw_name) == alias


def test_change_of_format_without_changes_of_identity_no_prevents_save(
    tmp_path: Path, alias: ContactAlias, input: dict[str, JsonValue]
) -> None:
    path = tmp_path / "aliases.json"
    save_json(path, {alias.raw_name: input})
    store = JsonAliasStore(path)
    path.write_text(path.read_text(encoding="utf-8").replace("\n", ""), encoding="utf-8")

    store.save()

    assert JsonAliasStore(path).find_by_name(alias.raw_name) == alias
