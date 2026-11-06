"""Validated JSON sources against the engine's current catalog."""

from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from client_automation.core.contracts import AliasStorePort, ContactReader, ExclusionListPort
from client_automation.core.matching import MatchingEngine
from client_automation.core.models import (
    AliasDestination,
    ClientCatalog,
    ClientLocation,
    ClientOccurrence,
    ClientPerson,
    ContactAlias,
    IgnoredContact,
    IgnoredReason,
    MatchMethod,
    PendingContact,
    PendingReason,
    ResolvedContact,
    SheetInfo,
    WeeklyContact,
    WeeklyResolution,
)
from client_automation.core.normalization import sorted_name_key
from client_automation.infra.json_alias_store import JsonAliasStore
from client_automation.infra.json_catalog_store import JsonCatalogStore
from client_automation.infra.json_contact_reader import JsonContactReader
from client_automation.infra.json_exclusion_list import JsonExclusionList
from client_automation.infra.json_resolution_store import JsonResolutionStore
from client_automation.infra.json_storage import save_json
from client_automation.infra.rapidfuzz_comparator import RapidFuzzComparator
from client_automation.services.matching import MatchingService


@pytest.fixture
def catalog() -> ClientCatalog:
    people = tuple(
        ClientPerson(
            person_id=identifier,
            sorted_name_key=sorted_name_key(name),
            canonical_name=name,
            holders=(name,),
            occurrences=(
                ClientOccurrence(location=ClientLocation(sheet="IOL", row=row), raw_name=name),
            ),
        )
        for identifier, name, row in (
            ("p0001", "Axa Sample", 30),
            ("p0002", "Notuka Example", 80),
        )
    )
    catalog = ClientCatalog(
        source_id="source_synthetic",
        revision="revision_current",
        generated_at=datetime(2026, 11, 5, tzinfo=UTC),
        sheets=(
            SheetInfo(
                name="IOL",
                sheet_id=0,
                name_column=1,
                first_week_column=None,
                last_week_column=None,
                last_client_row=100,
            ),
        ),
        people=people,
    )
    return catalog


@pytest.fixture
def engine(catalog: ClientCatalog) -> MatchingEngine:
    return MatchingEngine(catalog, RapidFuzzComparator())


def test_alias_reloaded_resolves_name_new_in_row_current(
    tmp_path: Path, engine: MatchingEngine
) -> None:
    path = tmp_path / "aliases.json"
    store: AliasStorePort = JsonAliasStore(path)
    store.register(
        ContactAlias(
            raw_name="Nickname previous",
            phone="+5491100000000",
            people=(AliasDestination(person_id="p0001", sheets=frozenset({"IOL"})),),
        )
    )
    store.save()
    store = JsonAliasStore(path)
    contact = WeeklyContact(
        raw_name="Name new (IOL)",
        phone="+5491100000000",
        last_activity_date=date(2026, 11, 5),
    )

    results = engine.resolve_contact(
        contact,
        aliases_phone=store.find_by_phone(contact.phone or ""),
        alias_name=store.find_by_name(contact.raw_name),
    )

    assert len(results) == 1
    result = results[0]
    assert isinstance(result, ResolvedContact)
    assert result.person_id == "p0001"
    assert result.method is MatchMethod.PHONE
    assert result.locations == (ClientLocation(sheet="IOL", row=30),)


@pytest.mark.parametrize("destinations_equivalent", [True, False])
def test_all_the_aliases_shared_reach_on_engine_for_decide(
    tmp_path: Path, engine: MatchingEngine, destinations_equivalent: bool
) -> None:
    path = tmp_path / "aliases.json"
    store: AliasStorePort = JsonAliasStore(path)
    for name, person_id in (
        ("Alias A", "p0001"),
        ("Alias B", "p0001" if destinations_equivalent else "p0002"),
    ):
        store.register(
            ContactAlias(
                raw_name=name,
                phone="+5491100000000",
                people=(AliasDestination(person_id=person_id, sheets=frozenset({"IOL"})),),
            )
        )
    store.save()
    store = JsonAliasStore(path)
    contact = WeeklyContact(
        raw_name="Other name", phone="+5491100000000", last_activity_date=date(2026, 11, 5)
    )

    results = engine.resolve_contact(
        contact, aliases_phone=store.find_by_phone(contact.phone or "")
    )

    assert len(results) == 1
    result = results[0]
    if destinations_equivalent:
        assert isinstance(result, ResolvedContact)
        assert result.person_id == "p0001"
        assert result.method is MatchMethod.PHONE
    else:
        assert isinstance(result, PendingContact)
        assert result.reason is PendingReason.AMBIGUOUS
        assert {candidate.person_id for candidate in result.candidates} == {"p0001", "p0002"}


def test_alias_learned_of_person_missing_requires_revision(
    tmp_path: Path, engine: MatchingEngine
) -> None:
    path = tmp_path / "aliases.json"
    store: AliasStorePort = JsonAliasStore(path)
    store.register(
        ContactAlias(
            raw_name="Nickname known",
            people=(AliasDestination(person_id="person_missing", sheets=frozenset({"IOL"})),),
        )
    )
    store.save()
    store = JsonAliasStore(path)
    contact = WeeklyContact(raw_name="Nickname known", last_activity_date=date(2026, 11, 5))

    results = engine.resolve_contact(contact, alias_name=store.find_by_name(contact.raw_name))

    assert len(results) == 1
    result = results[0]
    assert isinstance(result, PendingContact)
    assert result.reason is PendingReason.NO_MATCH


def test_exclusion_reloaded_has_priority_over_alias_by_phone(
    tmp_path: Path, engine: MatchingEngine
) -> None:
    path_aliases = tmp_path / "aliases.json"
    store: AliasStorePort = JsonAliasStore(path_aliases)
    store.register(
        ContactAlias(
            raw_name="Nickname previous",
            phone="+5491100000000",
            people=(AliasDestination(person_id="p0001", sheets=frozenset({"IOL"})),),
        )
    )
    store.save()
    store = JsonAliasStore(path_aliases)
    path_exclusions = tmp_path / "exclusions.json"
    require_list: ExclusionListPort = JsonExclusionList(path_exclusions)
    require_list.add("Call of Counter (IOL)")
    require_list = JsonExclusionList(path_exclusions)
    contact = WeeklyContact(
        raw_name="  CALL OF CÓUNTER (IOL)  ",
        phone="+5491100000000",
        last_activity_date=date(2026, 11, 5),
    )
    aliases = store.find_by_phone(contact.phone or "")

    without_exclusion = engine.resolve_contact(contact, aliases_phone=aliases)
    with_exclusion = engine.resolve_contact(
        contact, aliases_phone=aliases, excluded=require_list.contains(contact.raw_name)
    )

    assert len(without_exclusion) == len(with_exclusion) == 1
    assert isinstance(without_exclusion[0], ResolvedContact)
    result = with_exclusion[0]
    assert isinstance(result, IgnoredContact)
    assert result.reason is IgnoredReason.EXCLUDED


def test_input_weekly_json_resolves_and_persists_without_lose_groups_nor_dates(
    tmp_path: Path, engine: MatchingEngine
) -> None:
    path = tmp_path / "week.json"
    save_json(
        path,
        {
            "period": {"start_date": "2026-11-02", "end_date": "2026-11-06"},
            "extracted_at": "2026-11-06T18:00:00-03:00",
            "total_chats_scanned": 3,
            "contacts": [
                {"raw_name": "Axa Sample (IOL)", "last_activity_date": "2026-11-02"},
                {
                    "raw_name": "Group of sample",
                    "last_activity_date": "2026-11-06",
                    "is_group": True,
                },
                {"raw_name": "ZZZZZZZZZ", "last_activity_date": "2026-11-05"},
            ],
        },
    )
    reader: ContactReader = JsonContactReader(path)

    week = reader.read()
    results = tuple(engine.resolve_contact(contact)[0] for contact in week.contacts)

    resolved, ignored, pending = results
    assert isinstance(resolved, ResolvedContact)
    assert resolved.method is MatchMethod.EXACT
    assert resolved.locations == (ClientLocation(sheet="IOL", row=30),)
    assert resolved.contact.last_activity_date == date(2026, 11, 2)
    assert isinstance(ignored, IgnoredContact)
    assert ignored.reason is IgnoredReason.GROUP
    assert ignored.contact.last_activity_date == date(2026, 11, 6)
    assert isinstance(pending, PendingContact)
    assert pending.reason is PendingReason.NO_MATCH
    report = WeeklyResolution(
        resolved=tuple(result for result in results if isinstance(result, ResolvedContact)),
        pending=tuple(result for result in results if isinstance(result, PendingContact)),
        ignored=tuple(result for result in results if isinstance(result, IgnoredContact)),
        warnings=("Integration report",),
    )
    store = JsonResolutionStore(tmp_path / "output" / "report.json")
    store.save(report)

    assert store.read() == report


def test_catalog_reloaded_returns_all_the_rows_on_engine(
    tmp_path: Path, catalog: ClientCatalog
) -> None:
    person = catalog.people[0]
    person = replace(
        person,
        occurrences=(
            *person.occurrences,
            ClientOccurrence(location=ClientLocation(sheet="IOL", row=79), raw_name="Sample, Axa"),
        ),
    )
    catalog = replace(catalog, people=(person, *catalog.people[1:]))
    store = JsonCatalogStore(tmp_path / "catalog.json")
    store.save(catalog)
    reloaded = store.read()
    assert reloaded is not None
    engine = MatchingEngine(reloaded, RapidFuzzComparator())

    results = engine.resolve_contact(
        WeeklyContact(raw_name="Axa Sample (IOL)", last_activity_date=date(2026, 11, 5))
    )

    assert len(results) == 1
    result = results[0]
    assert isinstance(result, ResolvedContact)
    assert result.person_id == person.person_id
    assert result.method is MatchMethod.EXACT
    assert result.locations == (
        ClientLocation(sheet="IOL", row=30),
        ClientLocation(sheet="IOL", row=79),
    )


def test_service_dry_runs_without_files_and_real_connects_all_the_adapters(
    tmp_path: Path, catalog: ClientCatalog
) -> None:
    path_contacts = tmp_path / "week.json"
    path_catalog = tmp_path / "catalog.json"
    path_aliases = tmp_path / "state" / "aliases.json"
    path_exclusions = tmp_path / "state" / "exclusions.json"
    path_report = tmp_path / "output" / "report.json"
    save_json(
        path_contacts,
        {
            "period": {"start_date": "2026-11-02", "end_date": "2026-11-06"},
            "contacts": [
                {"raw_name": "Axa Sample (IOL)", "last_activity_date": "2026-11-05"},
                {"raw_name": "Group", "last_activity_date": "2026-11-05", "is_group": True},
                {"raw_name": "ZZZZZZZ", "last_activity_date": "2026-11-05"},
            ],
        },
    )
    catalogs = JsonCatalogStore(path_catalog)
    catalogs.save(catalog)
    original = (path_contacts.read_bytes(), path_catalog.read_bytes())
    aliases = JsonAliasStore(path_aliases)
    reports = JsonResolutionStore(path_report)
    service = MatchingService(
        reader_contacts=JsonContactReader(path_contacts),
        store_catalog=catalogs,
        store_aliases=aliases,
        list_exclusion=JsonExclusionList(path_exclusions),
        store_resolutions=reports,
        comparator=RapidFuzzComparator(),
    )

    simulated = service.run()

    assert len(simulated.resolved) == len(simulated.pending) == len(simulated.ignored) == 1
    assert aliases.find_by_name("Axa Sample (IOL)") is None
    assert not path_aliases.parent.exists()
    assert not path_report.parent.exists()
    real = service.run(dry_run=False)

    assert real == simulated
    assert reports.read() == real
    learned = JsonAliasStore(path_aliases).find_by_name("Axa Sample (IOL)")
    assert learned is not None
    assert learned.people == (AliasDestination(person_id="p0001", sheets=frozenset({"IOL"})),)
    assert not path_exclusions.exists()
    assert (path_contacts.read_bytes(), path_catalog.read_bytes()) == original
