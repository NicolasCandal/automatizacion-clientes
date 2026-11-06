"""Pure reconciliation of learned identities without files or logging."""

from dataclasses import replace

from .errors import DomainValidationError
from .models import (
    AliasDestination,
    ContactAlias,
    ContactResult,
    MatchMethod,
    ResolvedContact,
    WeeklyContact,
)


def reconcile_alias(
    existing: ContactAlias | None, new: ContactAlias
) -> tuple[ContactAlias, str | None]:
    """Return the retained association and a warning when conflicts occur."""
    if existing is None:
        return new, None
    if existing.raw_name != new.raw_name:
        raise DomainValidationError("Only aliases with the same original name can be reconciled.")
    if frozenset(existing.people) != frozenset(new.people):
        return (
            existing,
            "The alias already has different destinations. The previous association was preserved.",
        )
    if existing.phone is not None and new.phone is not None and existing.phone != new.phone:
        return (
            existing,
            "The alias already has a different phone number. "
            "The previous association was preserved.",
        )
    if existing.phone is None and new.phone is not None:
        return replace(existing, phone=new.phone), None
    return existing, None


def propose_alias(
    contact: WeeklyContact, results: tuple[ContactResult, ...]
) -> ContactAlias | None:
    """Propose learning only for contacts fully resolved through their names."""
    if not results:
        return None
    if any(result.contact != contact for result in results):
        raise DomainValidationError("Learned results must belong to the same contact.")
    destinations: list[AliasDestination] = []
    for result in results:
        if not isinstance(result, ResolvedContact) or result.method not in {
            MatchMethod.EXACT,
            MatchMethod.FUZZY,
        }:
            return None
        destinations.append(
            AliasDestination(
                person_id=result.person_id,
                sheets=frozenset(location.sheet for location in result.locations),
            )
        )
    return ContactAlias(raw_name=contact.raw_name, phone=contact.phone, people=tuple(destinations))
