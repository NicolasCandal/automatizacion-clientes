"""Complete workflow sharing snapshots across catalog, matching, and registration."""

import logging
from dataclasses import dataclass, field

from client_automation.core.contracts import (
    CatalogStore,
    ContactReader,
    ResolutionStore,
)
from client_automation.core.errors import ConcurrencyError
from client_automation.core.models import WeeklyResolution
from client_automation.core.registration import RegistrationResult
from client_automation.services.catalog import CatalogService
from client_automation.services.matching import MatchingService
from client_automation.services.registration import RegistrationService

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class AutomationResult:
    """Workflow report without registration when pending contacts block the plan."""

    dry_run: bool
    resolution: WeeklyResolution = field(repr=False)
    registration: RegistrationResult | None = field(repr=False)


class AutomationService:
    """Coordinate services without opening files or constructing their adapters."""

    def __init__(
        self,
        *,
        catalog: CatalogService,
        matching: MatchingService,
        registration: RegistrationService,
        reader_contacts: ContactReader,
        store_catalog: CatalogStore,
        store_resolutions: ResolutionStore,
    ) -> None:
        """Accept services and shared stores without executing operations."""
        self._catalog = catalog
        self._matches = matching
        self._registration = registration
        self._contacts = reader_contacts
        self._store_catalog = store_catalog
        self._reports = store_resolutions

    def run(
        self,
        *,
        header: str,
        dry_run: bool = True,
        allow_pending: bool = False,
        force_catalog: bool = False,
    ) -> AutomationResult:
        """Resolve one extraction and register only after successful planning."""
        catalog = self._catalog.run(dry_run=True, force=force_catalog)
        week = self._contacts.read()
        resolution = self._matches.run(catalog=catalog, week=week)
        if resolution.pending and not allow_pending:
            if not dry_run:
                self._store_catalog.save(catalog)
                self._reports.save(resolution)
            LOGGER.warning(
                "Registration requires review: %s pending results; no marks or aliases applied.",
                len(resolution.pending),
            )
            return AutomationResult(dry_run=dry_run, resolution=resolution, registration=None)
        plan = self._registration.plan(
            catalog=catalog,
            week=week,
            resolution=resolution,
            header=header,
            allow_pending=allow_pending,
        )
        for operation, previous in zip(plan.operations, plan.marks_previous, strict=True):
            LOGGER.info(
                "Sheet plan: %s; column=%s; new=%s; mark=%s; add=%s; remove=%s.",
                operation.sheet,
                operation.column,
                operation.new_week,
                len(operation.rows),
                len(operation.rows - previous),
                len(previous - operation.rows),
            )
        # Validate the plan before persisting auxiliary files.
        result = self._registration.run(plan)
        if not dry_run:
            self._store_catalog.save(catalog)
            persisted = self._matches.run(
                catalog=catalog,
                week=week,
                dry_run=False,
            )
            if persisted != resolution:
                raise ConcurrencyError("Decisions changed before registration. Resolve again.")
            result = self._registration.run(plan, dry_run=False)
        return AutomationResult(
            dry_run=dry_run,
            resolution=resolution,
            registration=result,
        )
