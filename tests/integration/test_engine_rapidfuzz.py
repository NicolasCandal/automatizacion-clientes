"""Pure engine integration with real RapidFuzz and a synthetic catalog."""

from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from client_automation.core.matching import MatchingEngine
from client_automation.core.models import (
    ClientCatalog,
    ClientLocation,
    ClientOccurrence,
    ClientPerson,
    MatchingPolicy,
    MatchMethod,
    PendingContact,
    PendingReason,
    ResolvedContact,
    SheetInfo,
    WeeklyContact,
)
from client_automation.core.normalization import sorted_name_key
from client_automation.infra.rapidfuzz_comparator import RapidFuzzComparator


def create_catalog(*names: str) -> ClientCatalog:
    people = tuple(
        ClientPerson(
            person_id=f"p{number:04d}",
            sorted_name_key=sorted_name_key(name),
            canonical_name=name,
            holders=(name,),
            occurrences=(
                ClientOccurrence(
                    location=ClientLocation(sheet="IOL", row=number + 2),
                    raw_name=name,
                ),
            ),
        )
        for number, name in enumerate(names, start=1)
    )
    return ClientCatalog(
        source_id="catalog_synthetic",
        revision="revision_1",
        generated_at=datetime(2026, 11, 5, tzinfo=UTC),
        sheets=(
            SheetInfo(
                name="IOL",
                sheet_id=0,
                name_column=1,
                first_week_column=None,
                last_week_column=None,
                last_client_row=len(names) + 2,
            ),
        ),
        people=people,
    )


def test_second_name_missing_resolves_by_comparison_fuzzy() -> None:
    engine = MatchingEngine(create_catalog("Firuso Baleve Fivora"), RapidFuzzComparator())
    contact = WeeklyContact(raw_name="FIRUSO FÍVORA (IOL)", last_activity_date=date(2026, 11, 5))

    results = engine.resolve_contact(contact)

    assert len(results) == 1
    result = results[0]
    assert isinstance(result, ResolvedContact)
    assert result.method is MatchMethod.FUZZY
    assert result.score == 100.0
    assert result.locations == (ClientLocation(sheet="IOL", row=3),)


def test_two_people_with_name_partial_shared_require_revision() -> None:
    engine = MatchingEngine(
        create_catalog("Mizote Sivivu Kukeve", "Mizote Sivivu Gibelo"), RapidFuzzComparator()
    )

    results = engine.resolve_contact(
        WeeklyContact(raw_name="Mizote Sivivu (IOL)", last_activity_date=date(2026, 11, 5))
    )

    result = results[0]
    assert isinstance(result, PendingContact)
    assert result.reason is PendingReason.AMBIGUOUS
    assert tuple(candidate.score for candidate in result.candidates) == (100.0, 100.0)


@pytest.mark.parametrize(
    ("query", "candidate", "reason", "score_expected"),
    [
        ("axa", "axxa", PendingReason.AMBIGUOUS, 85.71428571428571),
        ("axa", "axaxa", PendingReason.NO_MATCH, 75.0),
        ("zzz", "aaa", PendingReason.NO_MATCH, 0.0),
    ],
)
def test_scores_real_classify_without_trimming_previous(
    query: str, candidate: str, reason: PendingReason, score_expected: float
) -> None:
    engine = MatchingEngine(create_catalog(candidate), RapidFuzzComparator())

    result = engine.resolve_contact(
        WeeklyContact(raw_name=query, last_activity_date=date(2026, 11, 5))
    )[0]

    assert isinstance(result, PendingContact)
    assert result.reason is reason
    assert result.candidates[0].score == pytest.approx(score_expected)


def test_broker_incompatible_vetoes_one_score_perfect() -> None:
    engine = MatchingEngine(create_catalog("Firuso Baleve Fivora"), RapidFuzzComparator())

    result = engine.resolve_contact(
        WeeklyContact(raw_name="Firuso Fivora (Bull)", last_activity_date=date(2026, 11, 5))
    )[0]

    assert isinstance(result, PendingContact)
    assert result.reason is PendingReason.BROKER_MISMATCH
    assert result.candidates[0].score == 100.0


def test_many_variants_of_one_person_no_hide_to_its_competitor() -> None:
    catalog = create_catalog("Mizote Sivivu Kukeve", "Mizote Sivivu Gibelo")
    first = replace(
        catalog.people[0],
        holders=tuple(f"Mizote Sivivu Variant {number}" for number in range(12)),
    )
    engine = MatchingEngine(
        replace(catalog, people=(first, catalog.people[1])), RapidFuzzComparator()
    )

    result = engine.resolve_contact(
        WeeklyContact(raw_name="Mizote Sivivu", last_activity_date=date(2026, 11, 5))
    )[0]

    assert isinstance(result, PendingContact)
    assert result.reason is PendingReason.AMBIGUOUS
    assert len(result.candidates) == 2


def test_policy_configurable_applies_to_the_score_complete() -> None:
    engine = MatchingEngine(
        create_catalog("Axxa"),
        RapidFuzzComparator(),
        policy=MatchingPolicy(automatic_threshold=84.0, review_threshold=80.0),
    )

    result = engine.resolve_contact(
        WeeklyContact(raw_name="Axa", last_activity_date=date(2026, 11, 5))
    )[0]

    assert isinstance(result, ResolvedContact)
    assert result.method is MatchMethod.FUZZY
    assert result.score == pytest.approx(85.71428571428571)
