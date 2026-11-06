"""Stable identities, consistent reads, and catalog cache policy."""

import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from client_automation.core.catalog import build_catalog
from client_automation.core.contracts import CatalogStore, ClientReader, Clock
from client_automation.core.errors import (
    ConcurrencyError,
    ConfigurationError,
    DomainValidationError,
    PersistenceError,
    StructureError,
)
from client_automation.core.models import ClientCatalog, ClientRow, SheetInfo
from client_automation.services.catalog import CatalogService

INSTANT = datetime(2026, 11, 5, 18, 4, tzinfo=UTC)


@pytest.fixture
def sheets() -> tuple[SheetInfo, ...]:
    return (
        SheetInfo(
            name="IOL",
            sheet_id=0,
            name_column=1,
            first_week_column=3,
            last_week_column=10,
            last_client_row=20,
        ),
        SheetInfo(
            name="Bull",
            sheet_id=7,
            name_column=1,
            first_week_column=None,
            last_week_column=None,
            last_client_row=7,
        ),
        SheetInfo(
            name="PPI",
            sheet_id=2,
            name_column=1,
            first_week_column=None,
            last_week_column=None,
            last_client_row=2,
        ),
    )


@pytest.fixture
def clients() -> dict[str, tuple[ClientRow, ...]]:
    return {
        "IOL": (
            ClientRow(row=3, raw_name="  Fedezu, Lidexe Bekaxo  "),
            ClientRow(row=10, raw_name="Lidexe Bekaxo Fedezu"),
            ClientRow(row=20, raw_name="RAXOZI GOGOTE AND/OR RETUBO GEZIGE RATIXU"),
        ),
        "Bull": (
            ClientRow(row=3, raw_name="L\u00cdDEXE BEKAXO F\u00c9DEZU"),
            ClientRow(row=7, raw_name="MOPIVO BEMIBE (wife Gosoru Fezixi)"),
        ),
        "PPI": (),
    }


def build(
    sheets: tuple[SheetInfo, ...],
    clients: dict[str, tuple[ClientRow, ...]],
    previous: ClientCatalog | None = None,
) -> ClientCatalog:
    return build_catalog(
        source_id="source_synthetic",
        revision="revision_1",
        generated_at=INSTANT,
        sheets=sheets,
        clients_by_sheet=clients,
        previous=previous,
    )


@pytest.fixture
def catalog(
    sheets: tuple[SheetInfo, ...], clients: dict[str, tuple[ClientRow, ...]]
) -> ClientCatalog:
    return build(sheets, clients)


@dataclass
class CatalogEnvironment:
    reader: MagicMock
    store: MagicMock
    clock: MagicMock

    def service(self, source: str = "source_synthetic") -> CatalogService:
        return CatalogService(
            reader_clients=self.reader,
            store_catalog=self.store,
            clock=self.clock,
            source_id=source,
        )


@pytest.fixture
def environment(
    mocker: MockerFixture, sheets: tuple[SheetInfo, ...], clients: dict[str, tuple[ClientRow, ...]]
) -> CatalogEnvironment:
    environment = CatalogEnvironment(
        reader=mocker.create_autospec(ClientReader, instance=True, spec_set=True),
        store=mocker.create_autospec(CatalogStore, instance=True, spec_set=True),
        clock=mocker.create_autospec(Clock, instance=True, spec_set=True),
    )
    environment.store.read.return_value = None
    environment.reader.revision.return_value = "revision_1"
    environment.reader.list_sheets.return_value = sheets
    environment.reader.read_clients.side_effect = clients.__getitem__
    environment.clock.now.return_value = INSTANT
    return environment


def test_construction_preserves_all_the_rows_variants_and_holders(
    catalog: ClientCatalog,
) -> None:
    assert len(catalog.people) == 5
    assert tuple(sheet.name for sheet in catalog.sheets) == ("IOL", "Bull", "PPI")
    assert [person.sorted_name_key for person in catalog.people] == sorted(
        person.sorted_name_key for person in catalog.people
    )
    by_key = {person.sorted_name_key: person for person in catalog.people}
    fedezu = by_key["bekaxo fedezu lidexe"]
    assert fedezu.canonical_name == "Fedezu, Lidexe Bekaxo"
    assert len(fedezu.holders) == 3
    assert tuple((location.sheet, location.row) for location in fedezu.locations) == (
        ("IOL", 3),
        ("IOL", 10),
        ("Bull", 3),
    )
    assert fedezu.occurrences[0].raw_name == "  Fedezu, Lidexe Bekaxo  "
    assert by_key["gogote raxozi"].locations == by_key["gezige ratixu retubo"].locations
    assert by_key["fezixi gosoru"].locations[0].row == 7


def test_insert_client_no_renumbers_ids_previous(
    sheets: tuple[SheetInfo, ...],
    clients: dict[str, tuple[ClientRow, ...]],
    catalog: ClientCatalog,
) -> None:
    previous = replace(
        catalog,
        people=tuple(
            replace(person, person_id=f"p{number:04d}")
            for number, person in enumerate(catalog.people, start=1)
        ),
    )
    clients["IOL"] += (ClientRow(row=17, raw_name="AAA New Person"),)

    updated = build(sheets, clients, previous)

    previous_ids = {person.sorted_name_key: person.person_id for person in previous.people}
    current = {person.sorted_name_key: person.person_id for person in updated.people}
    assert all(current[key] == identifier for key, identifier in previous_ids.items())
    assert current["aaa new person"].startswith("p_")
    assert current["aaa new person"] not in previous_ids.values()


def test_ids_new_no_depend_of_order_nor_of_have_of_cache(
    sheets: tuple[SheetInfo, ...],
    clients: dict[str, tuple[ClientRow, ...]],
    catalog: ClientCatalog,
) -> None:
    reversed_clients = {sheet: tuple(reversed(rows)) for sheet, rows in clients.items()}
    rebuilt = build(tuple(reversed(sheets)), reversed_clients)
    assert {person.sorted_name_key: person.person_id for person in rebuilt.people} == {
        person.sorted_name_key: person.person_id for person in catalog.people
    }


def test_no_reuses_id_reserved_for_other_key(
    sheets: tuple[SheetInfo, ...],
    clients: dict[str, tuple[ClientRow, ...]],
    catalog: ClientCatalog,
) -> None:
    previous = replace(
        catalog,
        people=(replace(catalog.people[0], person_id=catalog.people[1].person_id),),
    )
    with pytest.raises(DomainValidationError, match="reserved"):
        build(sheets, clients, previous)


def test_ids_deterministic_separate_sources_different(
    sheets: tuple[SheetInfo, ...],
    clients: dict[str, tuple[ClientRow, ...]],
    catalog: ClientCatalog,
) -> None:
    other = build_catalog(
        source_id="other_source",
        revision="revision_1",
        generated_at=INSTANT,
        sheets=sheets,
        clients_by_sheet=clients,
    )

    assert {person.person_id for person in other.people}.isdisjoint(
        person.person_id for person in catalog.people
    )


def test_variants_of_same_holder_in_one_cell_no_duplicate_locations(
    sheets: tuple[SheetInfo, ...], clients: dict[str, tuple[ClientRow, ...]]
) -> None:
    clients["IOL"] = (ClientRow(row=3, raw_name="Axa Sample AND/OR Sample Axa"),)
    clients["Bull"] = ()
    catalog = build(sheets, clients)
    assert len(catalog.people) == 1
    assert catalog.people[0].holders == ("Axa Sample", "Sample Axa")
    assert len(catalog.people[0].locations) == 1


def test_holder_identical_in_multiple_rows_preserves_all_their_occurrences(
    sheets: tuple[SheetInfo, ...], clients: dict[str, tuple[ClientRow, ...]]
) -> None:
    clients["IOL"] = (
        ClientRow(row=3, raw_name="Axa Sample"),
        ClientRow(row=20, raw_name="Axa Sample"),
    )
    clients["Bull"] = (ClientRow(row=7, raw_name="Axa Sample"),)

    catalog = build(sheets, clients)

    assert len(catalog.people) == 1
    assert catalog.people[0].holders == ("Axa Sample",)
    assert len(catalog.people[0].occurrences) == 3


@pytest.mark.parametrize("change", ["sheet_missing", "sheet_extra", "row_duplicate", "row_outside"])
def test_rejects_read_of_structure_inconsistent(
    sheets: tuple[SheetInfo, ...], clients: dict[str, tuple[ClientRow, ...]], change: str
) -> None:
    if change == "sheet_missing":
        del clients["PPI"]
    elif change == "sheet_extra":
        clients["Other"] = ()
    elif change == "row_duplicate":
        clients["IOL"] += (clients["IOL"][0],)
    else:
        clients["IOL"] += (ClientRow(row=21, raw_name="Outside"),)
    with pytest.raises(DomainValidationError):
        build(sheets, clients)


@pytest.mark.parametrize("name", ["!!!", "(noise)", "Axa Sample AND/OR !!!"])
def test_no_discards_cells_or_holders_without_text_usable(
    sheets: tuple[SheetInfo, ...], clients: dict[str, tuple[ClientRow, ...]], name: str
) -> None:
    clients["IOL"] = (ClientRow(row=3, raw_name=name),)
    with pytest.raises(DomainValidationError):
        build(sheets, clients)


def test_core_rejects_catalog_previous_of_other_source(
    sheets: tuple[SheetInfo, ...],
    clients: dict[str, tuple[ClientRow, ...]],
    catalog: ClientCatalog,
) -> None:
    with pytest.raises(DomainValidationError, match="same source"):
        build(sheets, clients, replace(catalog, source_id="other"))


def test_constructor_no_runs_dependencies(environment: CatalogEnvironment) -> None:
    environment.service()
    assert not environment.reader.mock_calls
    assert not environment.store.mock_calls
    assert not environment.clock.mock_calls


def test_identifier_empty_rejects_without_reads(environment: CatalogEnvironment) -> None:
    with pytest.raises(ConfigurationError):
        environment.service(" ")
    assert not environment.reader.mock_calls


def test_dry_runs_by_default_without_persist_and_no_query_marks_or_headers(
    environment: CatalogEnvironment, catalog: ClientCatalog
) -> None:
    assert environment.service().run() == catalog
    environment.store.save.assert_not_called()
    environment.reader.read_marks.assert_not_called()
    environment.reader.last_week.assert_not_called()
    assert environment.reader.revision.call_count == 3
    environment.clock.now.assert_called_once()


def test_real_saves_only_catalog_complete(
    environment: CatalogEnvironment, catalog: ClientCatalog
) -> None:
    assert environment.service().run(dry_run=False) == catalog
    environment.store.save.assert_called_once_with(catalog)


def test_source_without_sheets_produces_one_catalog_empty_valid(
    environment: CatalogEnvironment,
) -> None:
    environment.reader.list_sheets.return_value = ()

    catalog = environment.service().run(dry_run=False)

    assert catalog.sheets == ()
    assert catalog.people == ()
    environment.reader.read_clients.assert_not_called()
    environment.store.save.assert_called_once_with(catalog)


def test_cache_validates_reuses_without_read_sheets_nor_query_clock(
    environment: CatalogEnvironment, catalog: ClientCatalog
) -> None:
    environment.store.read.return_value = catalog
    assert environment.service().run(dry_run=False) is catalog
    environment.reader.list_sheets.assert_not_called()
    environment.reader.read_clients.assert_not_called()
    environment.clock.now.assert_not_called()
    environment.store.save.assert_not_called()
    assert environment.reader.revision.call_count == 2


def test_rebuild_forced_preserves_ids_of_cache_validates(
    environment: CatalogEnvironment, catalog: ClientCatalog
) -> None:
    previous = replace(
        catalog,
        people=tuple(
            replace(person, person_id=f"p{number:04d}")
            for number, person in enumerate(catalog.people, start=1)
        ),
    )
    environment.store.read.return_value = previous
    rebuilt = environment.service().run(dry_run=False, force=True)
    assert rebuilt == previous
    environment.reader.list_sheets.assert_called_once()
    environment.store.save.assert_called_once_with(rebuilt)


def test_revision_different_rebuilds_without_lose_ids(
    environment: CatalogEnvironment, catalog: ClientCatalog
) -> None:
    environment.store.read.return_value = replace(catalog, revision="revision_previous")
    assert environment.service().run() == catalog
    environment.reader.list_sheets.assert_called_once()


@pytest.mark.parametrize("force", [True, False])
def test_cache_of_other_source_no_overwrites_although_forces(
    environment: CatalogEnvironment, catalog: ClientCatalog, force: bool
) -> None:
    environment.store.read.return_value = replace(catalog, source_id="other")
    with pytest.raises(ConfigurationError, match="other source"):
        environment.service().run(dry_run=False, force=force)
    assert not environment.reader.mock_calls
    environment.store.save.assert_not_called()


@pytest.mark.parametrize("force", [True, False])
@pytest.mark.parametrize("error", [StructureError, DomainValidationError])
def test_cache_invalid_only_rebuilds_with_forced_explicit(
    environment: CatalogEnvironment,
    catalog: ClientCatalog,
    force: bool,
    error: type[StructureError] | type[DomainValidationError],
) -> None:
    cause = error("Detail private")
    environment.store.read.side_effect = cause
    if force:
        assert environment.service().run(dry_run=False, force=True) == catalog
        environment.store.save.assert_called_once_with(catalog)
    else:
        with pytest.raises(error) as captured:
            environment.service().run(dry_run=False)
        assert captured.value is cause
        assert not environment.reader.mock_calls
        environment.store.save.assert_not_called()


def test_force_no_hides_failures_of_access_to_cache(environment: CatalogEnvironment) -> None:
    environment.store.read.side_effect = PersistenceError("access denied")
    with pytest.raises(PersistenceError):
        environment.service().run(dry_run=False, force=True)
    assert not environment.reader.mock_calls
    environment.store.save.assert_not_called()


def test_change_during_check_of_cache_prevents_reuse(
    environment: CatalogEnvironment, catalog: ClientCatalog
) -> None:
    environment.store.read.return_value = catalog
    environment.reader.revision.side_effect = ["revision_1", "revision_2"]
    with pytest.raises(ConcurrencyError):
        environment.service().run(dry_run=False)
    environment.store.save.assert_not_called()


@pytest.mark.parametrize(
    "revisions", [["revision_1", "revision_2"], ["revision_1", "revision_1", "revision_2"]]
)
def test_change_during_read_or_construction_prevents_saved(
    environment: CatalogEnvironment, revisions: list[str]
) -> None:
    environment.reader.revision.side_effect = revisions
    with pytest.raises(ConcurrencyError):
        environment.service().run(dry_run=False)
    environment.store.save.assert_not_called()


@pytest.mark.parametrize("revision", ["", " "])
def test_revision_empty_rejects_before_of_read_clients(
    environment: CatalogEnvironment, revision: str
) -> None:
    environment.reader.revision.return_value = revision
    with pytest.raises(StructureError):
        environment.service().run(dry_run=False)
    environment.reader.read_clients.assert_not_called()
    environment.store.save.assert_not_called()


def test_read_partial_failed_no_saves_catalog(environment: CatalogEnvironment) -> None:
    environment.reader.read_clients.side_effect = [(), PersistenceError("failure of read")]
    with pytest.raises(PersistenceError):
        environment.service().run(dry_run=False)
    environment.store.save.assert_not_called()
    environment.clock.now.assert_not_called()


def test_clock_without_zone_time_prevents_saved(environment: CatalogEnvironment) -> None:
    environment.clock.now.return_value = INSTANT.replace(tzinfo=None)
    with pytest.raises(DomainValidationError):
        environment.service().run(dry_run=False)
    environment.store.save.assert_not_called()


def test_failure_of_saved_propagates_without_retry_all_the_flow(
    environment: CatalogEnvironment,
) -> None:
    cause = PersistenceError("locked")
    environment.store.save.side_effect = cause
    with pytest.raises(PersistenceError) as captured:
        environment.service().run(dry_run=False)
    assert captured.value is cause
    environment.store.save.assert_called_once()
    environment.reader.list_sheets.assert_called_once()


def test_logs_no_expose_names_ids_revisions_or_exceptions(
    environment: CatalogEnvironment, caplog: pytest.LogCaptureFixture
) -> None:
    environment.store.read.side_effect = StructureError("Detail secret")
    with caplog.at_level(logging.INFO):
        environment.service().run(force=True)
    assert len(caplog.records) == 2
    for fragment in ("secret", "Fedezu", "revision_1", "source_synthetic", "p_"):
        assert fragment not in caplog.text
    assert all(registration.exc_info is None for registration in caplog.records)
