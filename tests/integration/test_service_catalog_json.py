"""Real cache persistence and matching after rows move."""

from dataclasses import replace
from datetime import UTC
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from client_automation.core.contracts import ClientReader
from client_automation.core.errors import StructureError
from client_automation.core.models import (
    AliasDestination,
    ClientLocation,
    ClientRow,
    ContactAlias,
    MatchMethod,
    SheetInfo,
)
from client_automation.infra.json_alias_store import JsonAliasStore
from client_automation.infra.json_catalog_store import JsonCatalogStore
from client_automation.infra.json_contact_reader import JsonContactReader
from client_automation.infra.json_exclusion_list import JsonExclusionList
from client_automation.infra.json_resolution_store import JsonResolutionStore
from client_automation.infra.json_storage import save_json
from client_automation.infra.rapidfuzz_comparator import RapidFuzzComparator
from client_automation.infra.system_clock import SystemClock
from client_automation.services.catalog import CatalogService
from client_automation.services.matching import MatchingService


@pytest.fixture
def reader(mocker: MockerFixture) -> MagicMock:
    reader = mocker.create_autospec(ClientReader, instance=True, spec_set=True)
    reader.revision.return_value = "revision_1"
    reader.list_sheets.return_value = (
        SheetInfo(
            name="IOL",
            sheet_id=0,
            name_column=1,
            first_week_column=None,
            last_week_column=None,
            last_client_row=3,
        ),
    )
    reader.read_clients.return_value = (ClientRow(row=3, raw_name="Axa Sample"),)
    return reader


def test_construction_persisted_preserves_alias_on_insert_client_and_move_row(
    tmp_path: Path, reader: MagicMock
) -> None:
    path_catalog = tmp_path / "state" / "catalog.json"
    store = JsonCatalogStore(path_catalog)
    service = CatalogService(
        reader_clients=reader,
        store_catalog=store,
        clock=SystemClock(),
        source_id="source_synthetic",
    )
    simulated = service.run()
    assert not path_catalog.parent.exists()
    assert simulated.generated_at.tzinfo is UTC
    first = service.run(dry_run=False)
    assert first.people == simulated.people
    assert store.read() == first
    content_cache = path_catalog.read_bytes()
    assert service.run(dry_run=False) == first
    assert path_catalog.read_bytes() == content_cache
    person_id = first.people[0].person_id
    path_aliases = tmp_path / "state" / "aliases.json"
    aliases = JsonAliasStore(path_aliases)
    aliases.register(
        ContactAlias(
            raw_name="Nickname known",
            people=(AliasDestination(person_id=person_id, sheets=frozenset({"IOL"})),),
        )
    )
    aliases.save()

    reader.revision.return_value = "revision_2"
    reader.list_sheets.return_value = (replace(first.sheets[0], last_client_row=5),)
    reader.read_clients.return_value = (
        ClientRow(row=3, raw_name="AAA New Person"),
        ClientRow(row=5, raw_name="Sample, Axa"),
    )
    updated = service.run(dry_run=False)
    assert store.read() == updated
    axa = next(person for person in updated.people if person.sorted_name_key == "axa sample")
    assert axa.person_id == person_id
    assert axa.locations == (ClientLocation(sheet="IOL", row=5),)
    path_contacts = tmp_path / "week.json"
    save_json(
        path_contacts,
        {
            "period": {"start_date": "2026-11-02", "end_date": "2026-11-06"},
            "contacts": [{"raw_name": "Nickname known", "last_activity_date": "2026-11-05"}],
        },
    )
    matching = MatchingService(
        reader_contacts=JsonContactReader(path_contacts),
        store_catalog=store,
        store_aliases=JsonAliasStore(path_aliases),
        list_exclusion=JsonExclusionList(tmp_path / "exclusions.json"),
        store_resolutions=JsonResolutionStore(tmp_path / "report.json"),
        comparator=RapidFuzzComparator(),
    )

    result = matching.run().resolved[0]

    assert result.method is MatchMethod.ALIAS
    assert result.person_id == person_id
    assert result.locations == (ClientLocation(sheet="IOL", row=5),)
    assert not (tmp_path / "report.json").exists()


def test_cache_corrupt_only_replaces_with_forced_and_execution_real(
    tmp_path: Path, reader: MagicMock
) -> None:
    path = tmp_path / "catalog.json"
    path.write_bytes(b"{corrupt")
    store = JsonCatalogStore(path)
    service = CatalogService(
        reader_clients=reader,
        store_catalog=store,
        clock=SystemClock(),
        source_id="source_synthetic",
    )
    with pytest.raises(StructureError):
        service.run(dry_run=False)
    service.run(force=True)
    assert path.read_bytes() == b"{corrupt"

    rebuilt = service.run(force=True, dry_run=False)

    assert store.read() == rebuilt
