"""Domain invariants with synthetic examples and no external access."""

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime

import pytest

from client_automation.core.errors import DomainValidationError
from client_automation.core.models import (
    ClientCatalog,
    ClientLocation,
    ClientOccurrence,
    ClientPerson,
    ClientRow,
    ContactResult,
    IgnoredContact,
    IgnoredReason,
    MatchCandidate,
    MatchMethod,
    PendingContact,
    PendingReason,
    ResolvedContact,
    SheetInfo,
    WeeklyContact,
    WeeklyContacts,
    WeeklyPeriod,
    WeeklyResolution,
)


@pytest.fixture
def period() -> WeeklyPeriod:
    return WeeklyPeriod(start_date=date(2026, 11, 2), end_date=date(2026, 11, 6))


@pytest.fixture
def contact() -> WeeklyContact:
    return WeeklyContact(
        raw_name="Sample contact (IOL)",
        last_activity_date=date(2026, 11, 5),
        phone="+5491100000000",
    )


@pytest.fixture
def location() -> ClientLocation:
    return ClientLocation(sheet="IOL", row=3)


@pytest.fixture
def person(location: ClientLocation) -> ClientPerson:
    return ClientPerson(
        person_id="p0001",
        sorted_name_key="client sample",
        canonical_name="Client Sample",
        holders=("Client Sample",),
        occurrences=(ClientOccurrence(location=location, raw_name="Client Sample"),),
    )


@pytest.fixture
def sheet() -> SheetInfo:
    return SheetInfo(
        name="IOL",
        sheet_id=0,
        name_column=1,
        first_week_column=3,
        last_week_column=10,
        last_client_row=30,
    )


@pytest.fixture
def catalog(sheet: SheetInfo, person: ClientPerson) -> ClientCatalog:
    return ClientCatalog(
        source_id="source_of_sample",
        revision="revision_1",
        generated_at=datetime(2026, 11, 5, 12, tzinfo=UTC),
        sheets=(sheet,),
        people=(person,),
    )


@pytest.fixture
def candidate(person: ClientPerson) -> MatchCandidate:
    return MatchCandidate(
        person_id=person.person_id,
        canonical_name=person.canonical_name,
        sheets=person.sheets,
        score=95.5,
        method=MatchMethod.FUZZY,
    )


def test_period_includes_endpoints_and_excludes_days_adjacent(period: WeeklyPeriod) -> None:
    assert period.contains(date(2026, 11, 2))
    assert period.contains(date(2026, 11, 6))
    assert not period.contains(date(2026, 11, 1))
    assert not period.contains(date(2026, 11, 7))


def test_period_accepts_one_only_day_and_change_of_year() -> None:
    same_day = WeeklyPeriod(start_date=date(2026, 11, 5), end_date=date(2026, 11, 5))
    change_year = WeeklyPeriod(start_date=date(2026, 12, 28), end_date=date(2027, 1, 1))

    assert same_day.contains(date(2026, 11, 5))
    assert change_year.contains(date(2027, 1, 1))


def test_period_rejects_period_reversed() -> None:
    with pytest.raises(DomainValidationError, match="end date"):
        WeeklyPeriod(start_date=date(2026, 11, 6), end_date=date(2026, 11, 2))


def test_period_no_accepts_instants_as_dates(period: WeeklyPeriod) -> None:
    instant = datetime(2026, 11, 5, tzinfo=UTC)

    with pytest.raises(DomainValidationError, match="without times"):
        replace(period, start_date=instant)
    with pytest.raises(DomainValidationError, match="without times"):
        replace(period, end_date=instant)
    with pytest.raises(DomainValidationError, match="include a time"):
        period.contains(instant)


def test_sheet_accepts_id_zero_and_absence_of_weeks(sheet: SheetInfo) -> None:
    empty = replace(sheet, first_week_column=None, last_week_column=None, last_client_row=2)

    assert empty.sheet_id == 0
    assert empty.first_week_column is None
    assert empty.last_client_row == 2


@pytest.mark.parametrize(
    ("first", "last"),
    [(None, 5), (3, None), (0, 5), (3, 0), (1, 5), (5, 3)],
)
def test_sheet_rejects_limits_weekly_inconsistent(
    sheet: SheetInfo, first: int | None, last: int | None
) -> None:
    with pytest.raises(DomainValidationError):
        replace(sheet, first_week_column=first, last_week_column=last)


@pytest.mark.parametrize("row", [0, -1, True])
def test_references_reject_rows_invalid(row: int) -> None:
    with pytest.raises(DomainValidationError, match="integer"):
        ClientRow(row=row, raw_name="Client Sample")
    with pytest.raises(DomainValidationError, match="integer"):
        ClientLocation(sheet="IOL", row=row)


def test_sheet_rejects_indices_invalid(sheet: SheetInfo) -> None:
    with pytest.raises(DomainValidationError, match="integer"):
        replace(sheet, sheet_id=-1)
    with pytest.raises(DomainValidationError, match="integer"):
        replace(sheet, name_column=0)
    with pytest.raises(DomainValidationError, match="integer"):
        replace(sheet, last_client_row=-1)


@pytest.mark.parametrize("last_row", [0, 2])
def test_catalog_rejects_locations_later_to_last_row(
    catalog: ClientCatalog, sheet: SheetInfo, last_row: int
) -> None:
    with pytest.raises(DomainValidationError, match="last row"):
        replace(catalog, sheets=(replace(sheet, last_client_row=last_row),))


def test_catalog_accepts_location_exactly_in_last_row(
    catalog: ClientCatalog, sheet: SheetInfo
) -> None:
    adjusted = replace(catalog, sheets=(replace(sheet, last_client_row=3),))

    assert adjusted.people[0].locations == (ClientLocation(sheet="IOL", row=3),)


@pytest.mark.parametrize("name", ["", "  ", "\n\t"])
def test_models_reject_texts_empty(name: str, location: ClientLocation) -> None:
    with pytest.raises(DomainValidationError, match="empty"):
        ClientRow(row=3, raw_name=name)
    with pytest.raises(DomainValidationError, match="empty"):
        ClientLocation(sheet=name, row=3)
    with pytest.raises(DomainValidationError, match="empty"):
        ClientOccurrence(location=location, raw_name=name)


def test_name_original_no_normalizes_nor_exposes_in_repr() -> None:
    original_name = "  SAMPLE, Client AND/OR Other Person  "
    row = ClientRow(row=3, raw_name=original_name)

    assert row.raw_name == original_name
    assert original_name not in repr(row)


def test_person_preserves_multiple_rows_of_one_sheet_and_multiple_brokers(
    person: ClientPerson,
) -> None:
    second_row = ClientOccurrence(
        location=ClientLocation(sheet="IOL", row=20), raw_name="Sample, Client"
    )
    other_broker = ClientOccurrence(
        location=ClientLocation(sheet="Bull", row=9), raw_name="Client Sample"
    )
    person_complete = replace(person, occurrences=(*person.occurrences, second_row, other_broker))

    assert person_complete.sheets == frozenset({"IOL", "Bull"})
    assert person_complete.locations == (
        ClientLocation(sheet="IOL", row=3),
        ClientLocation(sheet="IOL", row=20),
        ClientLocation(sheet="Bull", row=9),
    )


def test_catalog_accepts_holders_in_one_same_row(
    catalog: ClientCatalog, person: ClientPerson
) -> None:
    holder = replace(
        person,
        person_id="p0002",
        sorted_name_key="other person",
        canonical_name="Other Person",
        holders=("Other Person",),
    )
    set = replace(catalog, people=(person, holder))

    assert set.people[0].locations == set.people[1].locations


def test_person_requires_holders_and_occurrences(person: ClientPerson) -> None:
    with pytest.raises(DomainValidationError, match="holders and occurrences"):
        replace(person, holders=())
    with pytest.raises(DomainValidationError, match="holders and occurrences"):
        replace(person, occurrences=())


def test_person_rejects_holders_and_locations_duplicate(person: ClientPerson) -> None:
    with pytest.raises(DomainValidationError, match="duplicates"):
        replace(person, holders=("Client Sample", "Client Sample"))
    with pytest.raises(DomainValidationError, match="duplicates"):
        replace(person, occurrences=person.occurrences * 2)


def test_catalog_requires_date_with_zone_time(catalog: ClientCatalog) -> None:
    with pytest.raises(DomainValidationError, match="timezone-aware"):
        replace(catalog, generated_at=datetime(2026, 11, 5))


def test_catalog_rejects_sheets_repeated(catalog: ClientCatalog, sheet: SheetInfo) -> None:
    with pytest.raises(DomainValidationError, match="sheet names"):
        replace(catalog, sheets=(sheet, replace(sheet, sheet_id=1)))
    with pytest.raises(DomainValidationError, match="sheet identifiers"):
        replace(catalog, sheets=(sheet, replace(sheet, name="Bull")))


def test_catalog_rejects_identities_and_keys_duplicate(
    catalog: ClientCatalog, person: ClientPerson
) -> None:
    with pytest.raises(DomainValidationError, match="The people"):
        replace(catalog, people=(person, person))
    with pytest.raises(DomainValidationError, match="sorted name keys"):
        replace(catalog, people=(person, replace(person, person_id="p0002")))


def test_catalog_rejects_references_to_sheets_missing(catalog: ClientCatalog) -> None:
    with pytest.raises(DomainValidationError, match="sheet absent"):
        replace(catalog, sheets=())


def test_catalog_accepts_one_spreadsheet_without_people(catalog: ClientCatalog) -> None:
    empty = replace(catalog, people=())

    assert empty.people == ()


def test_contact_accepts_only_phone_and_hides_identity_in_repr(
    contact: WeeklyContact,
) -> None:
    without_name = replace(contact, raw_name="")

    assert without_name.phone == contact.phone
    assert contact.raw_name not in repr(contact)
    assert contact.phone is not None
    assert contact.phone not in repr(contact)


def test_contact_rejects_identity_missing_or_phone_empty(contact: WeeklyContact) -> None:
    with pytest.raises(DomainValidationError, match="name or phone number"):
        replace(contact, raw_name=" ", phone=None)
    with pytest.raises(DomainValidationError, match="phone"):
        replace(contact, phone=" ")


def test_contact_accepts_name_without_phone(contact: WeeklyContact) -> None:
    assert replace(contact, phone=None).raw_name == contact.raw_name


def test_contact_rejects_instants_without_convert_zone_time(
    contact: WeeklyContact,
) -> None:
    with pytest.raises(DomainValidationError, match="include a time"):
        replace(contact, last_activity_date=datetime(2026, 11, 5, tzinfo=UTC))


def test_week_accepts_zero_contacts_or_contacts_within_of_period(
    period: WeeklyPeriod, contact: WeeklyContact
) -> None:
    assert WeeklyContacts(period=period, contacts=()).contacts == ()
    assert WeeklyContacts(period=period, contacts=(contact,)).contacts == (contact,)


def test_week_rejects_contacts_outside_of_period(
    period: WeeklyPeriod, contact: WeeklyContact
) -> None:
    with_date_unrelated = replace(contact, last_activity_date=date(2026, 11, 1))

    with pytest.raises(DomainValidationError, match="reported period"):
        WeeklyContacts(period=period, contacts=(contact, with_date_unrelated))


@pytest.mark.parametrize("score", [-0.1, 100.1, float("nan"), float("inf"), -float("inf"), True])
def test_candidate_and_resolution_reject_scores_invalid(
    candidate: MatchCandidate,
    contact: WeeklyContact,
    location: ClientLocation,
    score: float,
) -> None:
    with pytest.raises(DomainValidationError, match="score"):
        replace(candidate, score=score)
    with pytest.raises(DomainValidationError, match="score"):
        ResolvedContact(
            contact=contact,
            person_id="p0001",
            locations=(location,),
            score=score,
            method=MatchMethod.FUZZY,
        )


@pytest.mark.parametrize("score", [0.0, 80.0, 92.0, 100.0])
def test_models_no_decide_thresholds_of_match(candidate: MatchCandidate, score: float) -> None:
    assert replace(candidate, score=score).score == score


def test_candidate_requires_sheets_with_name(candidate: MatchCandidate) -> None:
    with pytest.raises(DomainValidationError, match="at least one sheet"):
        replace(candidate, sheets=frozenset())
    with pytest.raises(DomainValidationError, match="empty"):
        replace(candidate, sheets=frozenset({" "}))


def test_resolved_preserves_all_the_rows_and_rejects_destinations_invalid(
    contact: WeeklyContact, person: ClientPerson, location: ClientLocation
) -> None:
    result = ResolvedContact(
        contact=contact,
        person_id=person.person_id,
        locations=(location, replace(location, row=20)),
        score=100.0,
        method=MatchMethod.EXACT,
    )

    assert len(result.locations) == 2
    with pytest.raises(DomainValidationError, match="destination location"):
        replace(result, locations=())
    with pytest.raises(DomainValidationError, match="duplicates"):
        replace(result, locations=(location, location))


def test_pending_accepts_absence_of_candidates_and_rejects_duplicates(
    contact: WeeklyContact, candidate: MatchCandidate
) -> None:
    pending = PendingContact(contact=contact, reason=PendingReason.NO_MATCH)

    assert pending.candidates == ()
    with pytest.raises(DomainValidationError, match="duplicates"):
        replace(pending, candidates=(candidate, replace(candidate, score=90.0)))


def test_results_have_states_different_and_reasons_typed(
    contact: WeeklyContact, location: ClientLocation
) -> None:
    resolved = ResolvedContact(
        contact=contact,
        person_id="p0001",
        locations=(location,),
        score=100.0,
        method=MatchMethod.ALIAS,
    )
    pending = PendingContact(contact=contact, reason=PendingReason.AMBIGUOUS)
    ignored = IgnoredContact(contact=contact, reason=IgnoredReason.EXCLUDED)
    results: tuple[ContactResult, ...] = (resolved, pending, ignored)

    assert tuple(result.kind for result in results) == (
        "resolved",
        "pending",
        "ignored",
    )
    assert not hasattr(pending, "locations")
    assert not hasattr(ignored, "person_id")


def test_resolution_accepts_holders_of_one_same_contact(
    contact: WeeklyContact, location: ClientLocation
) -> None:
    holder = ResolvedContact(
        contact=contact,
        person_id="p0001",
        locations=(location,),
        score=100.0,
        method=MatchMethod.PHONE,
    )
    joint_holder = replace(holder, person_id="p0002")
    resolution = WeeklyResolution(resolved=(holder, joint_holder), warnings=("Sample warning",))

    assert resolution.resolved[0].contact == resolution.resolved[1].contact
    assert resolution.resolved[0].person_id != resolution.resolved[1].person_id
    assert resolution.warnings == ("Sample warning",)
    assert WeeklyResolution().resolved == ()


def test_models_prevent_modify_the_identity(person: ClientPerson) -> None:
    with pytest.raises(FrozenInstanceError):
        person.__setattr__("person_id", "other_identity")


def test_codes_of_business_preserve_the_values_current() -> None:
    assert {method.value for method in MatchMethod} == {"phone", "alias", "exact", "fuzzy"}
    assert {reason.value for reason in PendingReason} == {
        "ambiguous",
        "no_match",
        "broker_mismatch",
    }
    assert {reason.value for reason in IgnoredReason} == {"group", "excluded"}
