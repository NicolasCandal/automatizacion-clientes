"""Matching decisions without file access, networking, or alias persistence."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from .brokers import DEFAULT_BROKER_TABLE, extract_brokers_and_name
from .contracts import NameComparator
from .errors import DomainValidationError
from .models import (
    ClientCatalog,
    ClientPerson,
    ContactAlias,
    ContactResult,
    IgnoredContact,
    IgnoredReason,
    MatchCandidate,
    MatchingPolicy,
    MatchMethod,
    PendingContact,
    PendingReason,
    ResolvedContact,
    WeeklyContact,
)
from .normalization import extract_holders, normalize, sorted_name_key


def _create_candidate(person: ClientPerson, score: float, method: MatchMethod) -> MatchCandidate:
    """Validate the score before sorting or discarding a candidate."""
    return MatchCandidate(
        person_id=person.person_id,
        canonical_name=person.canonical_name,
        sheets=person.sheets,
        score=score,
        method=method,
    )


class MatchingEngine:
    """Resolve contacts against a client snapshot and an injected comparator."""

    def __init__(
        self,
        catalog: ClientCatalog,
        comparator: NameComparator,
        *,
        policy: MatchingPolicy | None = None,
        table_brokers: Mapping[str, str] | None = None,
    ) -> None:
        """Build lookup indices once for the supplied snapshot."""
        self._people: Mapping[str, ClientPerson] = MappingProxyType(
            {person.person_id: person for person in catalog.people}
        )
        self._index_exact: Mapping[str, ClientPerson] = MappingProxyType(
            {person.sorted_name_key: person for person in catalog.people}
        )
        self._names: tuple[tuple[ClientPerson, tuple[str, ...]], ...] = tuple(
            (person, tuple(normalize(holder) for holder in person.holders))
            for person in catalog.people
        )
        self._comparator: NameComparator = comparator
        self._policy: MatchingPolicy = policy or MatchingPolicy()
        self._table_brokers: Mapping[str, str] = MappingProxyType(
            dict(DEFAULT_BROKER_TABLE if table_brokers is None else table_brokers)
        )

    def resolve_contact(
        self,
        contact: WeeklyContact,
        *,
        aliases_phone: tuple[ContactAlias, ...] = (),
        alias_name: ContactAlias | None = None,
        excluded: bool = False,
    ) -> tuple[ContactResult, ...]:
        """Return holder decisions without modifying the input records."""
        if contact.is_group:
            return (IgnoredContact(contact=contact, reason=IgnoredReason.GROUP),)
        if excluded:
            return (IgnoredContact(contact=contact, reason=IgnoredReason.EXCLUDED),)

        name = extract_brokers_and_name(contact.raw_name, self._table_brokers)
        sheets = frozenset(name.sheets)
        if aliases_phone:
            if contact.phone is None or any(
                alias.phone != contact.phone for alias in aliases_phone
            ):
                raise DomainValidationError(
                    "The supplied aliases do not match the contact's phone number."
                )
            return self._resolve_aliases(contact, aliases_phone, sheets, MatchMethod.PHONE)
        if alias_name is not None:
            if alias_name.raw_name != contact.raw_name:
                raise DomainValidationError("The supplied alias does not match the contact's name.")
            return self._resolve_aliases(contact, (alias_name,), sheets, MatchMethod.ALIAS)

        results: list[ContactResult] = []
        people_resolved: set[str] = set()
        for holder in extract_holders(name.name_without_parentheses) or (
            name.name_without_parentheses,
        ):
            result = self._resolve_holder(contact, holder, sheets)
            if isinstance(result, ResolvedContact):
                if result.person_id in people_resolved:
                    continue
                people_resolved.add(result.person_id)
            results.append(result)
        return tuple(results)

    def _candidates_for(self, holder: str) -> tuple[MatchCandidate, ...]:
        """Compare all holder variants and retain each person's best score."""
        query = normalize(holder)
        if not query:
            return ()
        exact = self._index_exact.get(sorted_name_key(holder))
        if exact is not None:
            return (_create_candidate(exact, 100.0, MatchMethod.EXACT),)

        candidates: list[MatchCandidate] = []
        for person, names in self._names:
            best: MatchCandidate | None = None
            for name in names:
                if not name:
                    continue
                candidate = _create_candidate(
                    person, self._comparator.similarity(query, name), MatchMethod.FUZZY
                )
                if best is None or candidate.score > best.score:
                    best = candidate
            if best is not None:
                candidates.append(best)
        return tuple(
            sorted(candidates, key=lambda candidate: (-candidate.score, candidate.person_id))
        )

    def _pending(
        self,
        contact: WeeklyContact,
        reason: PendingReason,
        candidates: tuple[MatchCandidate, ...],
    ) -> PendingContact:
        """Limit displayed suggestions only after deciding that review is required."""
        return PendingContact(
            contact=contact,
            reason=reason,
            candidates=candidates[: self._policy.suggestion_limit],
        )

    def _resolve_holder(
        self, contact: WeeklyContact, holder: str, sheets: frozenset[str]
    ) -> ResolvedContact | PendingContact:
        """Apply thresholds and broker vetoes without using brokers to break ties."""
        candidates = self._candidates_for(holder)
        if not candidates or candidates[0].score < self._policy.review_threshold:
            return self._pending(contact, PendingReason.NO_MATCH, candidates)

        best = candidates[0]
        tied = tuple(candidate for candidate in candidates if candidate.score == best.score)
        if sheets and not any(candidate.sheets & sheets for candidate in tied):
            return self._pending(contact, PendingReason.BROKER_MISMATCH, candidates)

        automatic = tuple(
            candidate
            for candidate in candidates
            if candidate.score >= self._policy.automatic_threshold
        )
        if len(tied) > 1 or len(automatic) != 1:
            return self._pending(contact, PendingReason.AMBIGUOUS, candidates)

        person = self._people[best.person_id]
        return ResolvedContact(
            contact=contact,
            person_id=person.person_id,
            locations=tuple(
                location for location in person.locations if not sheets or location.sheet in sheets
            ),
            score=best.score,
            method=best.method,
        )

    def _resolve_aliases(
        self,
        contact: WeeklyContact,
        aliases: tuple[ContactAlias, ...],
        sheets: frozenset[str],
        method: MatchMethod,
    ) -> tuple[ContactResult, ...]:
        """Validate shared aliases and destinations against the current catalog."""
        identifiers = sorted(
            {destination.person_id for alias in aliases for destination in alias.people}
        )
        candidates = tuple(
            _create_candidate(self._people[identifier], 100.0, method)
            for identifier in identifiers
            if identifier in self._people
        )
        destinations = frozenset(aliases[0].people)
        phone_alias = aliases[0].phone
        if (
            method is MatchMethod.ALIAS
            and phone_alias is not None
            and contact.phone is not None
            and phone_alias != contact.phone
        ):
            return (self._pending(contact, PendingReason.AMBIGUOUS, candidates),)
        if any(frozenset(alias.people) != destinations for alias in aliases):
            return (self._pending(contact, PendingReason.AMBIGUOUS, candidates),)
        if any(destination.person_id not in self._people for destination in destinations):
            return (self._pending(contact, PendingReason.NO_MATCH, candidates),)

        results: list[ContactResult] = []
        for destination in sorted(destinations, key=lambda destination: destination.person_id):
            person = self._people[destination.person_id]
            if not destination.sheets <= person.sheets:
                return (self._pending(contact, PendingReason.BROKER_MISMATCH, candidates),)
            locations = tuple(
                location
                for location in person.locations
                if location.sheet in destination.sheets and (not sheets or location.sheet in sheets)
            )
            if not locations:
                return (self._pending(contact, PendingReason.BROKER_MISMATCH, candidates),)
            results.append(
                ResolvedContact(
                    contact=contact,
                    person_id=person.person_id,
                    locations=locations,
                    score=100.0,
                    method=method,
                )
            )
        return tuple(results)
