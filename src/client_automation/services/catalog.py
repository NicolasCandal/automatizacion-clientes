"""Consistent source reads and explicit client catalog cache management."""

import logging

from client_automation.core.catalog import build_catalog
from client_automation.core.contracts import CatalogStore, ClientReader, Clock
from client_automation.core.errors import (
    ConcurrencyError,
    ConfigurationError,
    DomainValidationError,
    StructureError,
)
from client_automation.core.models import ClientCatalog

LOGGER = logging.getLogger(__name__)


class CatalogService:
    """Obtain a consistent snapshot without permitting spreadsheet writes."""

    def __init__(
        self,
        *,
        reader_clients: ClientReader,
        store_catalog: CatalogStore,
        clock: Clock,
        source_id: str,
    ) -> None:
        """Accept dependencies without performing external operations."""
        if not source_id.strip():
            raise ConfigurationError("A stable source identifier is required.")
        self._reader = reader_clients
        self._store = store_catalog
        self._clock = clock
        self._identifier_source = source_id

    def run(self, *, dry_run: bool = True, force: bool = False) -> ClientCatalog:
        """Reuse or rebuild the cache; default to a dry run without saving."""
        previous = self._read_cache(force=force)
        if previous is not None and previous.source_id != self._identifier_source:
            raise ConfigurationError(
                "The cache belongs to another source. Configure a separate catalog path."
            )
        revision = self._reader.revision()
        if not revision.strip():
            raise StructureError("The source must return a nonempty revision.")
        if previous is not None and previous.revision == revision and not force:
            self._check_revision(revision)
            LOGGER.info("Catalog cache reused: %s people.", len(previous.people))
            return previous
        sheets = self._reader.list_sheets()
        clients_by_sheet = {sheet.name: self._reader.read_clients(sheet.name) for sheet in sheets}
        self._check_revision(revision)
        catalog = build_catalog(
            source_id=self._identifier_source,
            revision=revision,
            generated_at=self._clock.now(),
            sheets=sheets,
            clients_by_sheet=clients_by_sheet,
            previous=previous,
        )
        self._check_revision(revision)
        if not dry_run:
            self._store.save(catalog)
        LOGGER.info(
            "Catalog built: dry_run=%s; %s people and %s sheets.",
            dry_run,
            len(catalog.people),
            len(catalog.sheets),
        )
        return catalog

    def _read_cache(self, *, force: bool) -> ClientCatalog | None:
        try:
            return self._store.read()
        except (StructureError, DomainValidationError):
            if not force:
                raise
            LOGGER.warning("The cache is invalid; rebuilding without recovering its previous IDs.")
            return None

    def _check_revision(self, expected_revision: str) -> None:
        if self._reader.revision() != expected_revision:
            raise ConcurrencyError("The source changed while building the catalog. Read it again.")
