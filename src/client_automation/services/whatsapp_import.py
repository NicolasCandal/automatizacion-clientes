"""Weekly import orchestration independent of files and WhatsApp access."""

import logging
from dataclasses import dataclass, field
from datetime import datetime

from client_automation.core.contracts import Clock, ContactExtractor, WeeklyContactsStore
from client_automation.core.errors import ConfigurationError
from client_automation.core.models import WeeklyPeriod
from client_automation.core.whatsapp_import import ContactExtraction

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class WhatsAppImportResult:
    """Validated extraction and execution mode without identities in repr."""

    extraction: ContactExtraction = field(repr=False)
    extracted_at: datetime
    dry_run: bool


class WhatsAppImportService:
    """Validate the full batch and save only when apply mode is requested."""

    def __init__(
        self, *, extractor: ContactExtractor, store: WeeklyContactsStore, clock: Clock
    ) -> None:
        """Accept ports without reads, writes, or clock queries."""
        self._extractor = extractor
        self._store = store
        self._clock = clock

    def run(self, period: WeeklyPeriod, *, dry_run: bool = True) -> WhatsAppImportResult:
        """Extract weekly contacts and optionally replace their input document."""
        extraction = self._extractor.extract(period)
        instant = self._clock.now()
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ConfigurationError("The import clock must be timezone-aware.")
        if not dry_run:
            self._store.save(extraction, extracted_at=instant)
        LOGGER.info(
            "Import completed: dry_run=%s; %s chats; %s contacts.",
            dry_run,
            extraction.total_chats_scanned,
            len(extraction.week.contacts),
        )
        return WhatsAppImportResult(extraction=extraction, extracted_at=instant, dry_run=dry_run)
