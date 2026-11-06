"""Regression tests for alias models."""

from dataclasses import replace

import pytest

from client_automation.core.errors import DomainValidationError
from client_automation.core.models import AliasDestination, ContactAlias


@pytest.fixture
def destination() -> AliasDestination:
    return AliasDestination(person_id="p0001", sheets=frozenset({"IOL", "Bull"}))


def test_alias_preserves_holders_and_sheets_without_persist_rows(
    destination: AliasDestination,
) -> None:
    holder = replace(destination, person_id="p0002")
    alias = ContactAlias(raw_name="Sample contact", people=(destination, holder))

    assert len(alias.people) == 2
    assert alias.people[0].sheets == frozenset({"IOL", "Bull"})
    assert not hasattr(alias.people[0], "row")
    assert alias.phone is None


def test_alias_accepts_phone_without_name_and_no_exposes_identity(
    destination: AliasDestination,
) -> None:
    alias = ContactAlias(raw_name="", phone="+5491100000000", people=(destination,))

    assert alias.raw_name == ""
    assert alias.phone == "+5491100000000"
    assert "+5491100000000" not in repr(alias)
    assert "p0001" not in repr(alias)


def test_alias_preserves_the_name_original(destination: AliasDestination) -> None:
    name = "  SAMPLE, Contact (IOL)  "
    alias = ContactAlias(raw_name=name, people=(destination,))

    assert alias.raw_name == name
    assert name not in repr(alias)


def test_alias_rejects_contact_without_identity(destination: AliasDestination) -> None:
    with pytest.raises(DomainValidationError, match="name or phone number"):
        ContactAlias(raw_name=" ", people=(destination,))
    with pytest.raises(DomainValidationError, match="phone"):
        ContactAlias(raw_name="Sample contact", phone=" ", people=(destination,))


def test_alias_requires_destinations_without_people_repeated(destination: AliasDestination) -> None:
    with pytest.raises(DomainValidationError, match="at least one person"):
        ContactAlias(raw_name="Sample contact", people=())
    with pytest.raises(DomainValidationError, match="duplicates"):
        ContactAlias(
            raw_name="Sample contact",
            people=(destination, replace(destination, sheets=frozenset({"Balanz"}))),
        )


def test_destination_requires_person_and_sheets_with_name(destination: AliasDestination) -> None:
    with pytest.raises(DomainValidationError, match="identifier"):
        replace(destination, person_id=" ")
    with pytest.raises(DomainValidationError, match="at least one sheet"):
        replace(destination, sheets=frozenset())
    with pytest.raises(DomainValidationError, match="alias sheet"):
        replace(destination, sheets=frozenset({" "}))
