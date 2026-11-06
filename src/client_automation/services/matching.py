"""Weekly orchestration with side-effect-free dry runs and deferred learning."""

import logging
from collections.abc import Mapping

from client_automation.core.aliases import propose_alias, reconcile_alias
from client_automation.core.contracts import (
    AliasStorePort,
    CatalogStore,
    ContactReader,
    ExclusionListPort,
    NameComparator,
    ResolutionStore,
)
from client_automation.core.errors import (
    AutomationError,
    ConcurrencyError,
    ConfigurationError,
)
from client_automation.core.matching import MatchingEngine
from client_automation.core.models import (
    ClientCatalog,
    ContactAlias,
    ContactResult,
    IgnoredContact,
    MatchingPolicy,
    PendingContact,
    ResolvedContact,
    WeeklyContact,
    WeeklyContacts,
    WeeklyResolution,
)

LOGGER = logging.getLogger(__name__)


class MatchingService:
    """Connect contact sources, the pure engine, and result persistence."""

    def __init__(
        self,
        *,
        reader_contacts: ContactReader,
        store_catalog: CatalogStore,
        store_aliases: AliasStorePort,
        list_exclusion: ExclusionListPort,
        store_resolutions: ResolutionStore,
        comparator: NameComparator,
        policy: MatchingPolicy | None = None,
        table_brokers: Mapping[str, str] | None = None,
    ) -> None:
        """Accept typed dependencies without performing reads or writes."""
        self._reader_contacts = reader_contacts
        self._store_catalog = store_catalog
        self._store_aliases = store_aliases
        self._list_exclusion = list_exclusion
        self._store_resolutions = store_resolutions
        self._comparator = comparator
        self._policy = policy
        self._table_brokers = None if table_brokers is None else dict(table_brokers)
        self._session_aliases_failed = False

    def run(
        self,
        *,
        dry_run: bool = True,
        catalog: ClientCatalog | None = None,
        week: WeeklyContacts | None = None,
    ) -> WeeklyResolution:
        """Resolve a week and optionally persist its report and learned associations."""
        if self._session_aliases_failed:
            raise ConfigurationError("Create a new service with a new session after alias failure.")
        if week is None:
            week = self._reader_contacts.read()
        if catalog is None:
            catalog = self._store_catalog.read()
        if catalog is None:
            raise ConfigurationError("No catalog exists. Build it from the source before matching.")
        engine = MatchingEngine(
            catalog, self._comparator, policy=self._policy, table_brokers=self._table_brokers
        )
        aliases_name: dict[str, ContactAlias | None] = {}
        aliases_phone: dict[str, tuple[ContactAlias, ...]] = {}
        results: list[ContactResult] = []
        proposals: list[ContactAlias] = []
        for contact in week.contacts:
            decisions = self._resolve_contact(engine, contact, aliases_name, aliases_phone)
            results.extend(decisions)
            proposal = propose_alias(contact, decisions)
            if proposal is not None:
                proposals.append(proposal)

        plan_aliases: dict[str, ContactAlias] = {}
        warnings: list[str] = []
        for proposal in proposals:
            name = proposal.raw_name
            existing = plan_aliases.get(name, aliases_name[name])
            preserved, warning = reconcile_alias(existing, proposal)
            if warning is not None:
                warnings.append(warning)
            if preserved != existing:
                plan_aliases[name] = preserved
        resolution = WeeklyResolution(
            resolved=tuple(value for value in results if isinstance(value, ResolvedContact)),
            pending=tuple(value for value in results if isinstance(value, PendingContact)),
            ignored=tuple(value for value in results if isinstance(value, IgnoredContact)),
            warnings=tuple(warnings),
        )
        if not dry_run:
            self._store_resolutions.save(resolution)
            self._persist_aliases(plan_aliases)
        LOGGER.info(
            "Matching completed: dry_run=%s; %s resolved, %s pending, %s ignored, and %s warnings.",
            dry_run,
            len(resolution.resolved),
            len(resolution.pending),
            len(resolution.ignored),
            len(resolution.warnings),
        )
        return resolution

    def _resolve_contact(
        self,
        engine: MatchingEngine,
        contact: WeeklyContact,
        aliases_name: dict[str, ContactAlias | None],
        aliases_phone: dict[str, tuple[ContactAlias, ...]],
    ) -> tuple[ContactResult, ...]:
        if contact.is_group:
            return engine.resolve_contact(contact)
        if self._list_exclusion.contains(contact.raw_name):
            return engine.resolve_contact(contact, excluded=True)
        by_phone: tuple[ContactAlias, ...] = ()
        if contact.phone is not None:
            if contact.phone not in aliases_phone:
                aliases_phone[contact.phone] = self._store_aliases.find_by_phone(contact.phone)
            by_phone = aliases_phone[contact.phone]
        by_name = None
        if not by_phone:
            if contact.raw_name not in aliases_name:
                aliases_name[contact.raw_name] = self._store_aliases.find_by_name(contact.raw_name)
            by_name = aliases_name[contact.raw_name]
        return engine.resolve_contact(contact, aliases_phone=by_phone, alias_name=by_name)

    def _persist_aliases(self, plan: Mapping[str, ContactAlias]) -> None:
        if not plan:
            return
        try:
            for alias in plan.values():
                if self._store_aliases.register(alias) is not None:
                    raise ConcurrencyError("An alias changed after planning. Open a new session.")
            self._store_aliases.save()
        except AutomationError:
            self._session_aliases_failed = True
            LOGGER.warning(
                "The report was saved but aliases could not be persisted. Open a new session."
            )
            raise
