"""Weekly input compatible with the reader and free of message bodies."""

import logging
from datetime import UTC, datetime
from pathlib import Path

from client_automation.core.errors import ConfigurationError, DomainValidationError
from client_automation.core.whatsapp_import import ContactExtraction
from client_automation.infra.json_storage import JsonValue, save_json

LOGGER = logging.getLogger(__name__)


class JsonWeeklyContactsStore:
    """Implement WeeklyContactsStore with atomic JSON replacement."""

    def __init__(self, path: Path, *, manifest: Path) -> None:
        """Validate destination paths without opening files or creating directories."""
        self._path = path.expanduser().resolve()
        input = manifest.expanduser().resolve()
        if self._path.suffix.lower() != ".json" or (
            self._path.is_relative_to(input) or input.is_relative_to(self._path)
        ):
            raise ConfigurationError("Weekly output must be JSON and separate from the manifest.")

    def save(self, extraction: ContactExtraction, *, extracted_at: datetime) -> None:
        """Save identities, dates, and counters while preserving prior output on failure."""
        if extracted_at.tzinfo is None or extracted_at.utcoffset() is None:
            raise DomainValidationError("The extraction timestamp must be timezone-aware.")
        week = extraction.week
        contacts: list[JsonValue] = [
            {
                "raw_name": contact.raw_name,
                "last_activity_date": contact.last_activity_date.isoformat(),
                "phone": contact.phone,
                "is_group": contact.is_group,
            }
            for contact in week.contacts
        ]
        document: dict[str, JsonValue] = {
            "period": {
                "start_date": week.period.start_date.isoformat(),
                "end_date": week.period.end_date.isoformat(),
            },
            "contacts": contacts,
            "extracted_at": extracted_at.astimezone(UTC).isoformat(),
            "total_chats_scanned": extraction.total_chats_scanned,
        }
        save_json(self._path, document)
        LOGGER.info("Weekly input saved: %s contacts.", len(week.contacts))
