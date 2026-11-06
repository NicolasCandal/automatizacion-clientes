"""Versioned catalogs, referential integrity, and cache recovery."""

import logging
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from client_automation.core.contracts import CatalogStore
from client_automation.core.errors import DomainValidationError, PersistenceError, StructureError
from client_automation.core.models import (
    ClientCatalog,
    ClientLocation,
    ClientOccurrence,
    ClientPerson,
    SheetInfo,
)
from client_automation.infra.json_catalog_store import JsonCatalogStore
from client_automation.infra.json_storage import JsonValue, read_json, save_json


@pytest.fixture
def occurrence_document() -> dict[str, JsonValue]:
    return {"sheet": "IOL", "row": 3, "raw_name": "Axa Sample"}


@pytest.fixture
def person_document(occurrence_document: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {
        "person_id": "p0042",
        "sorted_name_key": "axa sample",
        "canonical_name": "Axa Sample",
        "holders": ["Axa Sample"],
        "occurrences": [occurrence_document],
    }


@pytest.fixture
def sheet_document() -> dict[str, JsonValue]:
    return {
        "name": "IOL",
        "sheet_id": 0,
        "name_column": 1,
        "first_week_column": 3,
        "last_week_column": 10,
        "last_client_row": 80,
    }


@pytest.fixture
def document(
    sheet_document: dict[str, JsonValue], person_document: dict[str, JsonValue]
) -> dict[str, JsonValue]:
    return {
        "schema_version": 1,
        "source_id": "source_synthetic",
        "revision": "revision_1",
        "generated_at": "2026-11-05T18:04:00.123456-03:00",
        "sheets": [sheet_document],
        "people": [person_document],
    }


@pytest.fixture
def catalog() -> ClientCatalog:
    shared = ClientOccurrence(
        location=ClientLocation(sheet="IOL", row=3),
        raw_name="\u00c1xa Sample AND/OR Notuka Example",
    )
    return ClientCatalog(
        source_id="source private",
        revision="revision private",
        generated_at=datetime.fromisoformat("2026-11-05T18:04:00.123456-03:00"),
        sheets=(
            SheetInfo(
                name="Bull",
                sheet_id=7,
                name_column=1,
                first_week_column=None,
                last_week_column=None,
                last_client_row=9,
            ),
            SheetInfo(
                name="IOL",
                sheet_id=0,
                name_column=1,
                first_week_column=3,
                last_week_column=10,
                last_client_row=80,
            ),
        ),
        people=(
            ClientPerson(
                person_id="p0042",
                sorted_name_key="axa sample",
                canonical_name="\u00c1xa Sample",
                holders=("\u00c1xa Sample", "Sample, Axa"),
                occurrences=(
                    shared,
                    ClientOccurrence(
                        location=ClientLocation(sheet="IOL", row=80),
                        raw_name="  Sample, Axa  ",
                    ),
                    ClientOccurrence(
                        location=ClientLocation(sheet="Bull", row=9), raw_name="\u00c1xa Sample"
                    ),
                ),
            ),
            ClientPerson(
                person_id="p0007",
                sorted_name_key="example notuka",
                canonical_name="Notuka Example",
                holders=("Notuka Example",),
                occurrences=(shared,),
            ),
        ),
    )


def test_constructor_no_accesses_on_disk(tmp_path: Path, mocker: MockerFixture) -> None:
    path = tmp_path / "new" / "catalog.json"
    read = mocker.patch.object(Path, "read_text", side_effect=PermissionError("locked"))

    store: CatalogStore = JsonCatalogStore(path)

    assert store is not None
    read.assert_not_called()
    assert not path.parent.exists()


def test_absence_returns_none_without_create_directories(tmp_path: Path) -> None:
    path = tmp_path / "new" / "catalog.json"
    store: CatalogStore = JsonCatalogStore(path)

    assert store.read() is None
    assert not path.parent.exists()


def test_save_and_read_preserve_all_the_data_order_and_holders(
    tmp_path: Path, catalog: ClientCatalog
) -> None:
    path = tmp_path / "new" / "catalog.json"
    store: CatalogStore = JsonCatalogStore(path)

    store.save(catalog)
    reloaded = store.read()

    assert reloaded == catalog
    assert reloaded is not None
    assert tuple(sheet.name for sheet in reloaded.sheets) == ("Bull", "IOL")
    assert tuple(person.person_id for person in reloaded.people) == ("p0042", "p0007")
    assert reloaded.people[0].locations == (
        ClientLocation(sheet="IOL", row=3),
        ClientLocation(sheet="IOL", row=80),
        ClientLocation(sheet="Bull", row=9),
    )
    assert reloaded.people[0].locations[0] == reloaded.people[1].locations[0]
    assert reloaded.generated_at.microsecond == 123456
    assert reloaded.generated_at.utcoffset() == catalog.generated_at.utcoffset()
    assert list(path.parent.iterdir()) == [path]
    assert "\u00c1xa Sample" in path.read_text(encoding="utf-8")


def test_document_independent_converts_without_rewrite(
    tmp_path: Path, document: dict[str, JsonValue]
) -> None:
    path = tmp_path / "catalog.json"
    save_json(path, document)
    original = path.read_bytes()

    catalog = JsonCatalogStore(path).read()

    assert catalog is not None
    assert catalog.source_id == "source_synthetic"
    assert catalog.people[0].person_id == "p0042"
    assert catalog.people[0].holders == ("Axa Sample",)
    assert catalog.sheets[0].last_client_row == 80
    assert path.read_bytes() == original


def test_catalog_empty_is_different_of_file_missing(tmp_path: Path, catalog: ClientCatalog) -> None:
    path = tmp_path / "catalog.json"
    empty = replace(catalog, sheets=(), people=())
    store = JsonCatalogStore(path)
    store.save(empty)

    assert store.read() == empty


def test_saved_repeated_is_reproducible_and_no_regenerates_ids_or_dates(
    tmp_path: Path, catalog: ClientCatalog
) -> None:
    path = tmp_path / "catalog.json"
    store = JsonCatalogStore(path)
    store.save(catalog)
    original = path.read_bytes()
    store.save(catalog)

    assert path.read_bytes() == original
    assert store.read() == catalog


def test_save_allows_replace_cache_corrupt_after_rebuild(
    tmp_path: Path, catalog: ClientCatalog
) -> None:
    path = tmp_path / "catalog.json"
    path.write_bytes(b"{corrupt")

    JsonCatalogStore(path).save(catalog)

    assert JsonCatalogStore(path).read() == catalog


@pytest.mark.parametrize("content", [None, [], {}])
def test_document_incorrect_no_equals_to_file_missing(tmp_path: Path, content: JsonValue) -> None:
    path = tmp_path / "catalog.json"
    save_json(path, content)

    with pytest.raises(StructureError):
        JsonCatalogStore(path).read()


def test_catalog_legacy_requires_rebuild_without_modify(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    save_json(
        path,
        {
            "spreadsheet_id": "source_previous",
            "revision": "revision_1",
            "generated_at": "2026-11-05T18:04:00-03:00",
            "sheets": {"IOL": {"client_rows": 2}},
            "people": [],
        },
    )
    original = path.read_bytes()

    with pytest.raises(StructureError, match="Rebuild"):
        JsonCatalogStore(path).read()

    assert path.read_bytes() == original


@pytest.mark.parametrize("version", [None, True, 1.0, "1", 0, 2])
def test_version_requires_integer_exact_supported(
    tmp_path: Path, document: dict[str, JsonValue], version: JsonValue
) -> None:
    path = tmp_path / "catalog.json"
    document["schema_version"] = version
    save_json(path, document)

    with pytest.raises(StructureError, match="version"):
        JsonCatalogStore(path).read()


@pytest.mark.parametrize(
    ("level", "field"),
    [
        ("catalog", "revision"),
        ("sheet", "sheet_id"),
        ("person", "person_id"),
        ("occurrence", "row"),
    ],
)
@pytest.mark.parametrize("change", ["missing", "unknown"])
def test_rejects_fields_missing_or_unknown_in_any_level(
    tmp_path: Path,
    document: dict[str, JsonValue],
    sheet_document: dict[str, JsonValue],
    person_document: dict[str, JsonValue],
    occurrence_document: dict[str, JsonValue],
    level: str,
    field: str,
    change: str,
) -> None:
    inputs = {
        "catalog": document,
        "sheet": sheet_document,
        "person": person_document,
        "occurrence": occurrence_document,
    }
    if change == "missing":
        del inputs[level][field]
    else:
        inputs[level]["field_unknown"] = "value private"
    path = tmp_path / "catalog.json"
    save_json(path, document)
    original = path.read_bytes()

    with pytest.raises(StructureError):
        JsonCatalogStore(path).read()

    assert path.read_bytes() == original


@pytest.mark.parametrize("field", ["sheets", "people"])
def test_collections_of_catalog_require_lists(
    tmp_path: Path, document: dict[str, JsonValue], field: str
) -> None:
    path = tmp_path / "catalog.json"
    document[field] = {}
    save_json(path, document)

    with pytest.raises(StructureError):
        JsonCatalogStore(path).read()


@pytest.mark.parametrize("field", ["source_id", "revision", "generated_at"])
def test_fields_of_text_no_coerce(
    tmp_path: Path, document: dict[str, JsonValue], field: str
) -> None:
    path = tmp_path / "catalog.json"
    document[field] = 123
    save_json(path, document)

    with pytest.raises(StructureError):
        JsonCatalogStore(path).read()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sheet_id", True),
        ("name_column", "1"),
        ("last_client_row", 80.0),
        ("first_week_column", "3"),
        ("last_week_column", False),
    ],
)
def test_indices_require_integers_without_coercion(
    tmp_path: Path,
    document: dict[str, JsonValue],
    sheet_document: dict[str, JsonValue],
    field: str,
    value: JsonValue,
) -> None:
    path = tmp_path / "catalog.json"
    sheet_document[field] = value
    save_json(path, document)

    with pytest.raises(StructureError):
        JsonCatalogStore(path).read()


@pytest.mark.parametrize("field", ["holders", "occurrences"])
def test_collections_of_person_require_lists(
    tmp_path: Path,
    document: dict[str, JsonValue],
    person_document: dict[str, JsonValue],
    field: str,
) -> None:
    path = tmp_path / "catalog.json"
    person_document[field] = "require_list expected"
    save_json(path, document)

    with pytest.raises(StructureError):
        JsonCatalogStore(path).read()


@pytest.mark.parametrize("collection", ["sheets", "people"])
def test_elements_of_collections_require_objects(
    tmp_path: Path, document: dict[str, JsonValue], collection: str
) -> None:
    path = tmp_path / "catalog.json"
    document[collection] = [None]
    save_json(path, document)

    with pytest.raises(StructureError):
        JsonCatalogStore(path).read()


@pytest.mark.parametrize("instant", ["invalid date", "2026-11-05T18:04:00"])
def test_date_of_generation_requires_instant_valid_with_zone(
    tmp_path: Path, document: dict[str, JsonValue], instant: str
) -> None:
    path = tmp_path / "catalog.json"
    document["generated_at"] = instant
    save_json(path, document)

    with pytest.raises(DomainValidationError):
        JsonCatalogStore(path).read()


@pytest.mark.parametrize(
    "relationship", ["person", "key", "sheet", "sheet_id", "location", "holder"]
)
def test_rejects_identities_and_relationships_duplicate(
    tmp_path: Path,
    document: dict[str, JsonValue],
    sheet_document: dict[str, JsonValue],
    person_document: dict[str, JsonValue],
    occurrence_document: dict[str, JsonValue],
    relationship: str,
) -> None:
    if relationship == "person":
        document["people"] = [
            person_document,
            {**person_document, "sorted_name_key": "other key"},
        ]
    elif relationship == "key":
        document["people"] = [person_document, {**person_document, "person_id": "p0007"}]
    elif relationship == "sheet":
        document["sheets"] = [sheet_document, {**sheet_document, "sheet_id": 7}]
    elif relationship == "sheet_id":
        document["sheets"] = [sheet_document, {**sheet_document, "name": "Bull"}]
    elif relationship == "location":
        person_document["occurrences"] = [occurrence_document, occurrence_document]
    else:
        person_document["holders"] = ["Axa Sample", "Axa Sample"]
    path = tmp_path / "catalog.json"
    save_json(path, document)

    with pytest.raises(DomainValidationError):
        JsonCatalogStore(path).read()


@pytest.mark.parametrize("reference", ["sheet_missing", "row_outside_of_limit"])
def test_references_of_occurrences_must_correspond_to_the_structure(
    tmp_path: Path,
    document: dict[str, JsonValue],
    occurrence_document: dict[str, JsonValue],
    reference: str,
) -> None:
    occurrence_document["sheet" if reference == "sheet_missing" else "row"] = (
        "Unknown" if reference == "sheet_missing" else 81
    )
    path = tmp_path / "catalog.json"
    save_json(path, document)

    with pytest.raises(DomainValidationError):
        JsonCatalogStore(path).read()


def test_each_read_reflects_file_current_without_hide_corruption(
    tmp_path: Path, catalog: ClientCatalog
) -> None:
    path = tmp_path / "catalog.json"
    store = JsonCatalogStore(path)
    store.save(catalog)
    first = store.read()
    updated = replace(catalog, revision="revision_2")
    JsonCatalogStore(path).save(updated)

    assert store.read() == updated
    assert first == catalog
    path.write_bytes(b"{corrupt")
    with pytest.raises(StructureError):
        store.read()
    assert path.read_bytes() == b"{corrupt"


def test_failure_of_read_no_confuses_with_absence(tmp_path: Path, mocker: MockerFixture) -> None:
    cause = PermissionError("detail private")
    mocker.patch.object(Path, "read_text", side_effect=cause)

    with pytest.raises(PersistenceError) as captured:
        JsonCatalogStore(tmp_path / "catalog.json").read()

    assert captured.value.__cause__ is cause
    assert "private" not in str(captured.value)


@pytest.mark.parametrize("previous_file", [True, False])
def test_failure_of_saved_preserves_cache_and_allows_retry(
    tmp_path: Path, catalog: ClientCatalog, mocker: MockerFixture, previous_file: bool
) -> None:
    path = tmp_path / "catalog.json"
    store = JsonCatalogStore(path)
    if previous_file:
        store.save(catalog)
    updated = replace(catalog, revision="revision_2")
    cause = PermissionError("locked")
    replacement = mocker.patch.object(Path, "replace", side_effect=cause)

    with pytest.raises(PersistenceError) as captured:
        store.save(updated)

    assert captured.value.__cause__ is cause
    if previous_file:
        assert store.read() == catalog
        assert list(tmp_path.iterdir()) == [path]
    else:
        assert store.read() is None
        assert list(tmp_path.iterdir()) == []
    mocker.stop(replacement)
    store.save(updated)
    assert store.read() == updated


def test_schema_of_output_is_explicit_and_versioned(tmp_path: Path, catalog: ClientCatalog) -> None:
    path = tmp_path / "catalog.json"
    JsonCatalogStore(path).save(catalog)
    document = read_json(path)

    assert isinstance(document, dict)
    assert document["schema_version"] == 1
    assert set(document) == {
        "schema_version",
        "source_id",
        "revision",
        "generated_at",
        "sheets",
        "people",
    }


def test_logs_only_include_counts_without_paths_nor_identities(
    tmp_path: Path, catalog: ClientCatalog, caplog: pytest.LogCaptureFixture
) -> None:
    store = JsonCatalogStore(tmp_path / "catalog-private.json")

    with caplog.at_level(logging.INFO):
        store.save(catalog)
        store.read()

    assert len(caplog.records) == 2
    assert all("2 people and 2 sheets" in registration.message for registration in caplog.records)
    for identity in ("private", "\u00c1xa", "p0042", "Notuka"):
        assert identity not in caplog.text
