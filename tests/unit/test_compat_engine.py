from pathlib import Path

import pytest

from client_automation.infra.legacy_formats import (
    LegacyCatalog,
    LegacyOccurrence,
    LegacyPerson,
    LegacyResult,
)
from matching.alias_store import AliasStore
from matching.engine import build_index, resolve_contact, resolve_week
from matching.exclusion_list import ExclusionList


def _person(
    id_: str,
    sorted_name_key: str,
    canonical_name: str,
    holders: list[str],
    occurrences: list[LegacyOccurrence],
) -> LegacyPerson:
    return {
        "id": id_,
        "sorted_name_key": sorted_name_key,
        "canonical_name": canonical_name,
        "holders": holders,
        "occurrences": occurrences,
    }


def _catalog_base() -> LegacyCatalog:
    return {
        "people": [
            _person(
                "p0001",
                "baleve firuso fivora",
                "Firuso Baleve Fivora",
                ["Firuso Baleve Fivora"],
                [{"sheet": "IOL", "row": 27, "raw_name": "Firuso Baleve Fivora"}],
            ),
            _person(
                "p0002",
                "guremo lasise vurule",
                "Vurule, Lasise Guremo",
                ["Vurule, Lasise Guremo"],
                [{"sheet": "Bull", "row": 10, "raw_name": "Vurule, Lasise Guremo"}],
            ),
            _person(
                "p0003",
                "sisopa zokasu",
                "Zokasu, Sisopa",
                ["Zokasu, Sisopa"],
                [{"sheet": "IOL", "row": 55, "raw_name": "Zokasu, Sisopa"}],
            ),
            _person(
                "p0004",
                "gogote raxozi",
                "RAXOZI GOGOTE",
                ["RAXOZI GOGOTE"],
                [
                    {
                        "sheet": "Balanz",
                        "row": 4,
                        "raw_name": "RAXOZI GOGOTE AND/OR RETUBO GEZIGE RATIXU",
                    }
                ],
            ),
            _person(
                "p0005",
                "gezige ratixu retubo",
                "RETUBO GEZIGE RATIXU",
                ["RETUBO GEZIGE RATIXU"],
                [
                    {
                        "sheet": "Balanz",
                        "row": 4,
                        "raw_name": "RAXOZI GOGOTE AND/OR RETUBO GEZIGE RATIXU",
                    }
                ],
            ),
            # Both distinct people match the partial name through fuzzy comparison.
            # token_set_ratio sees the query as a subset of both names, scoring both at 100.
            _person(
                "p0006",
                "kukeve mizote sivivu",
                "Mizote Sivivu Kukeve",
                ["Mizote Sivivu Kukeve"],
                [{"sheet": "IOL", "row": 100, "raw_name": "Mizote Sivivu Kukeve"}],
            ),
            _person(
                "p0007",
                "gibelo mizote sivivu",
                "Mizote Sivivu Gibelo",
                ["Mizote Sivivu Gibelo"],
                [{"sheet": "Bull", "row": 200, "raw_name": "Mizote Sivivu Gibelo"}],
            ),
        ]
    }


@pytest.fixture
def catalog() -> LegacyCatalog:
    return _catalog_base()


@pytest.fixture
def alias_store(tmp_path: Path) -> AliasStore:
    return AliasStore(tmp_path / "aliases.json")


@pytest.fixture
def exclusion_list(tmp_path: Path) -> ExclusionList:
    return ExclusionList(tmp_path / "exclusion_list.json")


def _resolve_one(
    raw_name: str,
    catalog: LegacyCatalog,
    alias_store: AliasStore,
    exclusion_list: ExclusionList,
    phone: str | None = None,
    is_group: bool = False,
) -> list[LegacyResult]:
    idx = build_index(catalog)
    contact = {
        "raw_name": raw_name,
        "last_activity_date": "2026-07-20",
        "phone": phone,
        "is_group": is_group,
    }
    return resolve_contact(contact, idx, alias_store, exclusion_list)


def test_exact_with_broker_correct(
    catalog: LegacyCatalog, alias_store: AliasStore, exclusion_list: ExclusionList
) -> None:
    results = _resolve_one("Firuso Baleve Fivora (IOL)", catalog, alias_store, exclusion_list)
    assert len(results) == 1
    r = results[0]
    assert r["kind"] == "resolved"
    assert r["person_id"] == "p0001"
    assert r["method"] == "exact"
    assert r["sheets"] == ["IOL"]
    assert r["row_by_sheet"] == {"IOL": 27}


def test_format_surname_comma_name_matches_equal(
    catalog: LegacyCatalog, alias_store: AliasStore, exclusion_list: ExclusionList
) -> None:
    results = _resolve_one("ZOKASU, SISOPA (IOL)", catalog, alias_store, exclusion_list)
    assert results[0]["person_id"] == "p0003"
    assert results[0]["method"] == "exact"


def test_order_of_name_and_surname_reversed(
    catalog: LegacyCatalog, alias_store: AliasStore, exclusion_list: ExclusionList
) -> None:
    results = _resolve_one("Lasise Guremo Vurule (Bull)", catalog, alias_store, exclusion_list)
    assert results[0]["person_id"] == "p0002"
    assert results[0]["method"] == "exact"


def test_ownership_shared_matches_by_one_only_holder(
    catalog: LegacyCatalog, alias_store: AliasStore, exclusion_list: ExclusionList
) -> None:
    results = _resolve_one("Gogote Raxozi (Balanz)", catalog, alias_store, exclusion_list)
    assert len(results) == 1
    assert results[0]["person_id"] == "p0004"
    assert results[0]["sheets"] == ["Balanz"]
    assert results[0]["row_by_sheet"] == {"Balanz": 4}


def test_fuzzy_ignores_second_name_of_more(
    catalog: LegacyCatalog, alias_store: AliasStore, exclusion_list: ExclusionList
) -> None:
    # A missing middle-name token still scores 100 with token_set_ratio.
    # A simple ratio would instead produce a lower score.
    results = _resolve_one("Firuso Fivora (IOL)", catalog, alias_store, exclusion_list)
    assert results[0]["kind"] == "resolved"
    assert results[0]["person_id"] == "p0001"
    assert results[0]["method"] == "fuzzy"
    assert results[0]["score"] == 100


def test_broker_no_matches_goes_to_revision_although_name_of_100(
    catalog: LegacyCatalog, alias_store: AliasStore, exclusion_list: ExclusionList
) -> None:
    # This person occurs only in IOL; an incompatible Bull hint must prevent resolution.
    results = _resolve_one("Firuso Baleve Fivora (Bull)", catalog, alias_store, exclusion_list)
    assert results[0]["kind"] == "pending"
    assert results[0]["reason"] == "broker_mismatch"
    assert results[0]["candidates"][0]["person_id"] == "p0001"


def test_ambiguous_two_candidates_with_100(
    catalog: LegacyCatalog, alias_store: AliasStore, exclusion_list: ExclusionList
) -> None:
    results = _resolve_one("Mizote Sivivu", catalog, alias_store, exclusion_list)
    assert results[0]["kind"] == "pending"
    assert results[0]["reason"] == "ambiguous"
    ids_with_100 = {c["person_id"] for c in results[0]["candidates"] if c["score"] == 100}
    assert ids_with_100 == {"p0006", "p0007"}


def test_without_match_client_new(
    catalog: LegacyCatalog, alias_store: AliasStore, exclusion_list: ExclusionList
) -> None:
    results = _resolve_one("Tevosi No_Surname Unknown", catalog, alias_store, exclusion_list)
    assert results[0]["kind"] == "pending"
    assert results[0]["reason"] == "no_match"


def test_group_ignores(
    catalog: LegacyCatalog, alias_store: AliasStore, exclusion_list: ExclusionList
) -> None:
    results = _resolve_one("Family group", catalog, alias_store, exclusion_list, is_group=True)
    assert results[0]["kind"] == "ignored"
    assert results[0]["reason"] == "group"


def test_list_exclusion_ignores(
    catalog: LegacyCatalog, alias_store: AliasStore, exclusion_list: ExclusionList
) -> None:
    exclusion_list.add("The Counter")
    results = _resolve_one("The Counter", catalog, alias_store, exclusion_list)
    assert results[0]["kind"] == "ignored"
    assert results[0]["reason"] == "excluded"


def test_resolved_persists_in_alias_store(
    catalog: LegacyCatalog, alias_store: AliasStore, exclusion_list: ExclusionList
) -> None:
    _resolve_one("Firuso Baleve Fivora (IOL)", catalog, alias_store, exclusion_list)
    input = alias_store.find_by_name("Firuso Baleve Fivora (IOL)")
    assert input is not None
    assert input["people"] == [{"person_id": "p0001", "sheets": ["IOL"]}]


def test_second_time_resolves_by_alias_without_recompute(
    catalog: LegacyCatalog, alias_store: AliasStore, exclusion_list: ExclusionList
) -> None:
    _resolve_one("Firuso Baleve Fivora (IOL)", catalog, alias_store, exclusion_list)
    # This name cannot match directly but already has a learned alias.
    alias_store.register("Nickname Odd", [{"person_id": "p0001", "sheets": ["IOL"]}])
    results = _resolve_one("Nickname Odd", catalog, alias_store, exclusion_list)
    assert results[0]["kind"] == "resolved"
    assert results[0]["method"] == "alias"
    assert results[0]["person_id"] == "p0001"


def test_phone_resolves_although_name_no_matches(
    catalog: LegacyCatalog, alias_store: AliasStore, exclusion_list: ExclusionList
) -> None:
    alias_store.register(
        "Firuso IOL old", [{"person_id": "p0001", "sheets": ["IOL"]}], "+5491155551234"
    )
    results = _resolve_one(
        "Name Completely Different",
        catalog,
        alias_store,
        exclusion_list,
        phone="+5491155551234",
    )
    assert results[0]["kind"] == "resolved"
    assert results[0]["method"] == "phone"
    assert results[0]["person_id"] == "p0001"


def test_alias_no_overwrites_in_silence(
    catalog: LegacyCatalog, alias_store: AliasStore, exclusion_list: ExclusionList
) -> None:
    alias_store.register("Contact X", [{"person_id": "p0001", "sheets": ["IOL"]}])
    warning = alias_store.register("Contact X", [{"person_id": "p0099", "sheets": ["Bull"]}])
    assert warning is not None
    # The existing association was not overwritten.
    preserved = alias_store.find_by_name("Contact X")
    assert preserved is not None
    assert preserved["people"] == [{"person_id": "p0001", "sheets": ["IOL"]}]


def test_resolve_week_groups_correctly(
    catalog: LegacyCatalog, alias_store: AliasStore, exclusion_list: ExclusionList
) -> None:
    contacts = [
        {
            "raw_name": "Firuso Baleve Fivora (IOL)",
            "last_activity_date": "2026-07-20",
            "phone": None,
            "is_group": False,
        },
        {
            "raw_name": "Tevosi Unknown",
            "last_activity_date": "2026-07-20",
            "phone": None,
            "is_group": False,
        },
        {
            "raw_name": "Group Friends",
            "last_activity_date": "2026-07-20",
            "phone": None,
            "is_group": True,
        },
    ]
    resolution = resolve_week(contacts, catalog, alias_store, exclusion_list)
    assert len(resolution["resolved"]) == 1
    assert len(resolution["pending"]) == 1
    assert len(resolution["ignored"]) == 1
