"""Matching decisions with controlled comparisons and no networking or files."""

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from client_automation.core.errors import DomainValidationError
from client_automation.core.matching import MatchingEngine
from client_automation.core.models import (
    AliasDestination,
    ClientCatalog,
    ClientLocation,
    ClientOccurrence,
    ClientPerson,
    ContactAlias,
    ContactResult,
    IgnoredContact,
    IgnoredReason,
    MatchingPolicy,
    MatchMethod,
    PendingContact,
    PendingReason,
    ResolvedContact,
    SheetInfo,
    WeeklyContact,
)
from client_automation.core.normalization import normalize, sorted_name_key


class ControlledComparator:
    """Regression tests for matching."""

    def __init__(self, scores: Mapping[tuple[str, str], float] | None = None) -> None:
        self.scores: Mapping[tuple[str, str], float] = scores or {}
        self.queries: list[tuple[str, str]] = []

    def similarity(self, query_name: str, candidate_name: str) -> float:
        self.queries.append((query_name, candidate_name))
        return self.scores.get((query_name, candidate_name), 0.0)


def create_person(person_id: str, name: str, sheet: str = "IOL") -> ClientPerson:
    return ClientPerson(
        person_id=person_id,
        sorted_name_key=sorted_name_key(name),
        canonical_name=name,
        holders=(name,),
        occurrences=(ClientOccurrence(location=ClientLocation(sheet=sheet, row=3), raw_name=name),),
    )


def create_catalog(people: tuple[ClientPerson, ...]) -> ClientCatalog:
    sheet_names = sorted({sheet for person in people for sheet in person.sheets})
    return ClientCatalog(
        source_id="source_synthetic",
        revision="revision_1",
        generated_at=datetime(2026, 11, 5, tzinfo=UTC),
        sheets=tuple(
            SheetInfo(
                name=name,
                sheet_id=identifier,
                name_column=1,
                first_week_column=None,
                last_week_column=None,
                last_client_row=100,
            )
            for identifier, name in enumerate(sheet_names)
        ),
        people=people,
    )


def resolved_unique(results: tuple[ContactResult, ...]) -> ResolvedContact:
    assert len(results) == 1
    result = results[0]
    assert isinstance(result, ResolvedContact)
    return result


def pending_unique(results: tuple[ContactResult, ...]) -> PendingContact:
    assert len(results) == 1
    result = results[0]
    assert isinstance(result, PendingContact)
    return result


@pytest.fixture
def contact() -> WeeklyContact:
    return WeeklyContact(
        raw_name="Axa Sample", last_activity_date=date(2026, 11, 5), phone="+5491100000000"
    )


@pytest.fixture
def person() -> ClientPerson:
    person = create_person("p0001", "Axa Radono Sample")
    return replace(
        person,
        occurrences=(
            *person.occurrences,
            ClientOccurrence(
                location=ClientLocation(sheet="IOL", row=10), raw_name="Sample Axa Radono"
            ),
            ClientOccurrence(
                location=ClientLocation(sheet="Bull", row=5), raw_name="Axa Radono Sample"
            ),
        ),
    )


@pytest.fixture
def alias(contact: WeeklyContact) -> ContactAlias:
    return ContactAlias(
        raw_name=contact.raw_name,
        phone=contact.phone,
        people=(AliasDestination(person_id="p0001", sheets=frozenset({"IOL"})),),
    )


@pytest.mark.parametrize("suffix", ["", " (IOL/Bull)", " (client usual)"])
def test_exact_preserves_all_the_rows_without_use_comparator(
    person: ClientPerson, contact: WeeklyContact, suffix: str
) -> None:
    comparator = ControlledComparator()
    engine = MatchingEngine(create_catalog((person,)), comparator)
    result = resolved_unique(
        engine.resolve_contact(replace(contact, raw_name=f"SAMPLE, AXA RÁDONO{suffix}"))
    )

    assert result.person_id == person.person_id
    assert result.locations == person.locations
    assert result.method is MatchMethod.EXACT
    assert comparator.queries == []


def test_broker_restricts_sheets_without_lose_rows_repeated(
    person: ClientPerson, contact: WeeklyContact
) -> None:
    engine = MatchingEngine(create_catalog((person,)), ControlledComparator())
    result = resolved_unique(
        engine.resolve_contact(replace(contact, raw_name="Axa Radono Sample (IOL)"))
    )

    assert result.locations == (
        ClientLocation(sheet="IOL", row=3),
        ClientLocation(sheet="IOL", row=10),
    )


def test_exact_with_broker_incompatible_requires_revision(
    person: ClientPerson, contact: WeeklyContact
) -> None:
    engine = MatchingEngine(create_catalog((person,)), ControlledComparator())
    result = pending_unique(
        engine.resolve_contact(replace(contact, raw_name="Axa Radono Sample (PPI)"))
    )

    assert result.reason is PendingReason.BROKER_MISMATCH
    assert result.candidates[0].score == 100.0


@pytest.mark.parametrize(
    ("score", "reason"),
    [
        (0.0, PendingReason.NO_MATCH),
        (79.99, PendingReason.NO_MATCH),
        (80.0, PendingReason.AMBIGUOUS),
        (91.99, PendingReason.AMBIGUOUS),
        (92.0, None),
        (100.0, None),
    ],
)
def test_limits_of_the_thresholds(
    person: ClientPerson,
    contact: WeeklyContact,
    score: float,
    reason: PendingReason | None,
) -> None:
    comparator = ControlledComparator({("axa sample", "axa radono sample"): score})
    engine = MatchingEngine(create_catalog((person,)), comparator)
    results = engine.resolve_contact(contact)

    if reason is None:
        result = resolved_unique(results)
        assert result.method is MatchMethod.FUZZY
        assert result.score == score
    else:
        assert pending_unique(results).reason is reason
    assert comparator.queries == [("axa sample", "axa radono sample")]


@pytest.mark.parametrize("scores", [(96.0, 94.0), (100.0, 100.0)])
def test_multiple_people_strong_are_ambiguous_although_the_broker_only_confirms_one(
    contact: WeeklyContact, scores: tuple[float, float]
) -> None:
    people = (
        create_person("p0001", "First Person"),
        create_person("p0002", "Second Person", "Bull"),
    )
    comparator = ControlledComparator(
        {
            ("axa sample", "first person"): scores[0],
            ("axa sample", "second person"): scores[1],
        }
    )
    engine = MatchingEngine(create_catalog(people), comparator)
    result = pending_unique(engine.resolve_contact(replace(contact, raw_name="Axa Sample (IOL)")))

    assert result.reason is PendingReason.AMBIGUOUS
    assert len(result.candidates) == 2


def test_best_candidate_of_other_broker_no_replaces_by_one_more_weak(
    contact: WeeklyContact,
) -> None:
    people = (
        create_person("p0001", "First Person", "Bull"),
        create_person("p0002", "Second Person"),
    )
    comparator = ControlledComparator(
        {
            ("axa sample", "first person"): 99.0,
            ("axa sample", "second person"): 85.0,
        }
    )
    engine = MatchingEngine(create_catalog(people), comparator)

    assert (
        pending_unique(engine.resolve_contact(replace(contact, raw_name="Axa Sample (IOL)"))).reason
        is PendingReason.BROKER_MISMATCH
    )


def test_limit_of_suggestions_no_hides_competitors_strong(contact: WeeklyContact) -> None:
    first = create_person("p0001", "First Person")
    first = replace(first, holders=tuple(f"Variant {number}" for number in range(12)))
    second = create_person("p0002", "Second Person")
    scores = {("axa sample", normalize(holder)): 96.0 for holder in first.holders}
    scores[("axa sample", "second person")] = 94.0
    comparator = ControlledComparator(scores)
    engine = MatchingEngine(
        create_catalog((first, second)),
        comparator,
        policy=MatchingPolicy(suggestion_limit=1),
    )
    result = pending_unique(engine.resolve_contact(contact))

    assert result.reason is PendingReason.AMBIGUOUS
    assert len(result.candidates) == 1
    assert len(comparator.queries) == 13


def test_best_variant_of_one_person_no_creates_ambiguity(
    person: ClientPerson, contact: WeeklyContact
) -> None:
    person = replace(person, holders=("Axa Radono Sample", "Sample Axa"))
    comparator = ControlledComparator(
        {
            ("axa sample", "axa radono sample"): 95.0,
            ("axa sample", "sample axa"): 99.0,
        }
    )
    result = resolved_unique(
        MatchingEngine(create_catalog((person,)), comparator).resolve_contact(contact)
    )

    assert result.score == 99.0


def test_suggestions_tied_have_order_deterministic(contact: WeeklyContact) -> None:
    people = (
        create_person("p0002", "Second Person"),
        create_person("p0001", "First Person"),
    )
    comparator = ControlledComparator(
        {
            ("axa sample", "first person"): 85.0,
            ("axa sample", "second person"): 85.0,
        }
    )
    result = pending_unique(
        MatchingEngine(create_catalog(people), comparator).resolve_contact(contact)
    )

    assert tuple(candidate.person_id for candidate in result.candidates) == ("p0001", "p0002")


@pytest.mark.parametrize("name", ["--- (IOL)", "(IOL)"])
def test_name_without_text_comparable_no_resolves(
    person: ClientPerson, contact: WeeklyContact, name: str
) -> None:
    comparator = ControlledComparator()
    result = pending_unique(
        MatchingEngine(create_catalog((person,)), comparator).resolve_contact(
            replace(contact, raw_name=name)
        )
    )

    assert result.reason is PendingReason.NO_MATCH
    assert result.candidates == ()
    assert comparator.queries == []


def test_catalog_empty_or_holders_no_comparable_no_generate_candidates(
    person: ClientPerson, contact: WeeklyContact
) -> None:
    comparator = ControlledComparator()
    for people in ((), (replace(person, holders=("---",)),)):
        result = pending_unique(
            MatchingEngine(create_catalog(people), comparator).resolve_contact(contact)
        )
        assert result.candidates == ()
    assert comparator.queries == []


@pytest.mark.parametrize("is_group", [True, False])
def test_groups_and_exclusions_have_priority_without_compare(
    person: ClientPerson, contact: WeeklyContact, alias: ContactAlias, is_group: bool
) -> None:
    comparator = ControlledComparator()
    results = MatchingEngine(create_catalog((person,)), comparator).resolve_contact(
        replace(contact, is_group=is_group), alias_name=alias, excluded=True
    )

    assert len(results) == 1
    assert isinstance(results[0], IgnoredContact)
    assert results[0].reason is (IgnoredReason.GROUP if is_group else IgnoredReason.EXCLUDED)
    assert comparator.queries == []


def test_holders_resolve_by_separate_and_no_duplicate_people(
    person: ClientPerson, contact: WeeklyContact
) -> None:
    other_person = create_person("p0002", "Daresu Zeta")
    engine = MatchingEngine(create_catalog((person, other_person)), ControlledComparator())
    results = engine.resolve_contact(
        replace(contact, raw_name="Axa Radono Sample / Daresu Zeta / SAMPLE, AXA RADONO")
    )

    assert len(results) == 2
    assert all(isinstance(result, ResolvedContact) for result in results)


def test_holder_pending_no_converts_in_resolved(
    person: ClientPerson, contact: WeeklyContact
) -> None:
    results = MatchingEngine(create_catalog((person,)), ControlledComparator()).resolve_contact(
        replace(contact, raw_name="Axa Radono Sample / Unknown Person")
    )

    assert len(results) == 2
    assert isinstance(results[0], ResolvedContact)
    assert isinstance(results[1], PendingContact)


def test_alias_name_uses_rows_current_without_compare(
    person: ClientPerson, contact: WeeklyContact, alias: ContactAlias
) -> None:
    comparator = ControlledComparator()
    result = resolved_unique(
        MatchingEngine(create_catalog((person,)), comparator).resolve_contact(
            contact, alias_name=alias
        )
    )

    assert result.method is MatchMethod.ALIAS
    assert tuple(location.row for location in result.locations) == (3, 10)
    assert comparator.queries == []


def test_phone_overrides_over_alias_by_name(
    person: ClientPerson, contact: WeeklyContact, alias: ContactAlias
) -> None:
    other_person = create_person("p0002", "Daresu Zeta")
    alias_by_name = replace(
        alias, people=(AliasDestination(person_id="p0002", sheets=frozenset({"IOL"})),)
    )
    result = resolved_unique(
        MatchingEngine(
            create_catalog((person, other_person)), ControlledComparator()
        ).resolve_contact(
            contact,
            aliases_phone=(replace(alias, raw_name="Name previous"),),
            alias_name=alias_by_name,
        )
    )

    assert result.person_id == "p0001"
    assert result.method is MatchMethod.PHONE


@pytest.mark.parametrize("phone", [None, "+5491199999999"])
def test_evidence_of_phone_unrelated_rejects(
    person: ClientPerson, contact: WeeklyContact, alias: ContactAlias, phone: str | None
) -> None:
    engine = MatchingEngine(create_catalog((person,)), ControlledComparator())

    with pytest.raises(DomainValidationError, match="contact's phone number"):
        engine.resolve_contact(replace(contact, phone=phone), aliases_phone=(alias,))


def test_evidence_of_name_unrelated_rejects(
    person: ClientPerson, contact: WeeklyContact, alias: ContactAlias
) -> None:
    engine = MatchingEngine(create_catalog((person,)), ControlledComparator())

    with pytest.raises(DomainValidationError, match="contact's name"):
        engine.resolve_contact(contact, alias_name=replace(alias, raw_name="Other name"))


def test_alias_by_name_with_phone_different_requires_revision(
    person: ClientPerson, contact: WeeklyContact, alias: ContactAlias
) -> None:
    result = pending_unique(
        MatchingEngine(create_catalog((person,)), ControlledComparator()).resolve_contact(
            contact, alias_name=replace(alias, phone="+5491199999999")
        )
    )

    assert result.reason is PendingReason.AMBIGUOUS


@pytest.mark.parametrize("destinations_match", [True, False])
def test_phone_shared_resolves_only_with_destinations_consistent(
    person: ClientPerson,
    contact: WeeklyContact,
    alias: ContactAlias,
    destinations_match: bool,
) -> None:
    other_person = create_person("p0002", "Daresu Zeta")
    destination_other = AliasDestination(person_id="p0002", sheets=frozenset({"IOL"}))
    first = replace(alias, people=(*alias.people, destination_other))
    second = replace(
        alias,
        raw_name="Other contact",
        people=tuple(reversed(first.people)) if destinations_match else (destination_other,),
    )
    results = MatchingEngine(
        create_catalog((person, other_person)), ControlledComparator()
    ).resolve_contact(contact, aliases_phone=(first, second))

    if destinations_match:
        assert len(results) == 2
        assert all(isinstance(result, ResolvedContact) for result in results)
    else:
        assert pending_unique(results).reason is PendingReason.AMBIGUOUS


@pytest.mark.parametrize(
    ("person_id", "sheets", "reason"),
    [
        ("missing", frozenset({"IOL"}), PendingReason.NO_MATCH),
        ("p0001", frozenset({"PPI"}), PendingReason.BROKER_MISMATCH),
    ],
)
def test_alias_stale_goes_to_revision_without_resolution_partial(
    person: ClientPerson,
    contact: WeeklyContact,
    alias: ContactAlias,
    person_id: str,
    sheets: frozenset[str],
    reason: PendingReason,
) -> None:
    stale = replace(alias, people=(AliasDestination(person_id=person_id, sheets=sheets),))
    result = pending_unique(
        MatchingEngine(create_catalog((person,)), ControlledComparator()).resolve_contact(
            contact, alias_name=stale
        )
    )

    assert result.reason is reason


def test_alias_no_ignores_one_broker_explicit_incompatible(
    person: ClientPerson, contact: WeeklyContact, alias: ContactAlias
) -> None:
    contact = replace(contact, raw_name="Axa Sample (Bull)")
    result = pending_unique(
        MatchingEngine(create_catalog((person,)), ControlledComparator()).resolve_contact(
            contact, aliases_phone=(alias,)
        )
    )

    assert result.reason is PendingReason.BROKER_MISMATCH


@pytest.mark.parametrize("score", [float("nan"), float("inf"), -1.0, 101.0, True])
def test_comparator_no_can_hide_score_invalid_in_one_variant_less(
    person: ClientPerson, contact: WeeklyContact, score: float
) -> None:
    person = replace(person, holders=("Axa Radono Sample", "Other variant"))
    comparator = ControlledComparator(
        {
            ("axa sample", "axa radono sample"): 100.0,
            ("axa sample", "other variant"): score,
        }
    )

    with pytest.raises(DomainValidationError, match="score"):
        MatchingEngine(create_catalog((person,)), comparator).resolve_contact(contact)


def test_policy_and_table_of_brokers_are_configurable(
    person: ClientPerson, contact: WeeklyContact
) -> None:
    table = {"own": "IOL"}
    engine = MatchingEngine(
        create_catalog((person,)),
        ControlledComparator({("axa sample", "axa radono sample"): 95.0}),
        policy=MatchingPolicy(automatic_threshold=98.0, review_threshold=90.0),
        table_brokers=table,
    )
    table["own"] = "PPI"
    result = pending_unique(engine.resolve_contact(replace(contact, raw_name="Axa Sample (own)")))

    assert result.reason is PendingReason.AMBIGUOUS


@pytest.mark.parametrize(
    ("automatic", "revision", "limit"),
    [
        (80.0, 80.0, 3),
        (80.0, 92.0, 3),
        (101.0, 80.0, 3),
        (92.0, float("nan"), 3),
        (92.0, 80.0, 0),
        (92.0, 80.0, True),
    ],
)
def test_policy_rejects_thresholds_or_limits_invalid(
    automatic: float, revision: float, limit: int
) -> None:
    with pytest.raises(DomainValidationError):
        MatchingPolicy(
            automatic_threshold=automatic, review_threshold=revision, suggestion_limit=limit
        )
