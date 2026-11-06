"""Legacy facade delegating every matching decision to MatchingEngine."""

from collections.abc import Sequence
from dataclasses import dataclass, field

from client_automation.core.aliases import propose_alias
from client_automation.core.matching import MatchingEngine
from client_automation.infra.legacy_formats import (
    LegacyResolution,
    LegacyResult,
    catalog_for_matching,
    contact_from_legacy,
    result_to_legacy,
)
from client_automation.infra.rapidfuzz_comparator import RapidFuzzComparator

from .alias_store import AliasStore
from .exclusion_list import ExclusionList


@dataclass(frozen=True, slots=True)
class MatchIndex:
    """Opaque legacy index that does not represent a source revision."""

    engine: MatchingEngine = field(repr=False)


def build_index(catalog: object) -> MatchIndex:
    """Validate people and occurrences before constructing the matching engine."""
    return MatchIndex(MatchingEngine(catalog_for_matching(catalog), RapidFuzzComparator()))


def resolve_contact(
    contact: object, idx: MatchIndex, alias_store: AliasStore, exclusion_list: ExclusionList
) -> list[LegacyResult]:
    """Adapt legacy contacts and learn only fully resolved associations."""
    identity = contact_from_legacy(contact)
    decisions = idx.engine.resolve_contact(
        identity,
        aliases_phone=()
        if identity.phone is None
        else alias_store.store.find_by_phone(identity.phone),
        alias_name=alias_store.store.find_by_name(identity.raw_name),
        excluded=exclusion_list.contains(identity.raw_name),
    )
    proposal = propose_alias(identity, decisions)
    warning = None if proposal is None else alias_store.store.register(proposal)
    results = [result_to_legacy(decision) for decision in decisions]
    if warning is not None:
        for result in results:
            result["warnings"] = [warning]
    return results


def resolve_week(
    contacts: Sequence[object],
    catalog: object,
    alias_store: AliasStore,
    exclusion_list: ExclusionList,
) -> LegacyResolution:
    """Validate every decision before learning or saving any associations."""
    catalog_index = build_index(catalog)
    identities = tuple(contact_from_legacy(contact) for contact in contacts)
    decisions = tuple(
        catalog_index.engine.resolve_contact(
            contact,
            aliases_phone=()
            if contact.phone is None
            else alias_store.store.find_by_phone(contact.phone),
            alias_name=alias_store.store.find_by_name(contact.raw_name),
            excluded=exclusion_list.contains(contact.raw_name),
        )
        for contact in identities
    )
    proposals = [
        proposal
        for contact, results in zip(identities, decisions, strict=True)
        if (proposal := propose_alias(contact, results)) is not None
    ]
    warnings = [
        warning
        for proposal in proposals
        if (warning := alias_store.store.register(proposal)) is not None
    ]
    result: LegacyResolution = {
        "resolved": [],
        "pending": [],
        "ignored": [],
        "warnings": warnings,
    }
    for results in decisions:
        for decision in results:
            adapted = result_to_legacy(decision)
            adapted.pop("kind", None)
            if decision.kind == "resolved":
                result["resolved"].append(adapted)
            elif decision.kind == "pending":
                result["pending"].append(adapted)
            else:
                result["ignored"].append(adapted)
    if proposals:
        alias_store.save()
    return result
