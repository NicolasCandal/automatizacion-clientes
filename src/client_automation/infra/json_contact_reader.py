"""Strict conversion of weekly JSON input into domain models."""

import logging
from datetime import date, datetime
from pathlib import Path

from client_automation.core.errors import DomainValidationError, StructureError
from client_automation.core.models import WeeklyContact, WeeklyContacts, WeeklyPeriod
from client_automation.infra.json_storage import JsonValue, read_json

LOGGER = logging.getLogger(__name__)


class JsonContactReader:
    """Implement ContactReader for the documented extraction format."""

    def __init__(self, path: Path) -> None:
        """Accept the configured path without opening files or creating directories."""
        self._path = path

    def read(self) -> WeeklyContacts:
        """Read and validate the complete document before returning weekly contacts."""
        document = read_json(self._path)
        if not isinstance(document, dict) or not {"period", "contacts"} <= document.keys() <= {
            "period",
            "contacts",
            "extracted_at",
            "total_chats_scanned",
        }:
            raise StructureError("The weekly document has missing or unknown fields.")
        period_document = document["period"]
        if not isinstance(period_document, dict) or period_document.keys() != {
            "start_date",
            "end_date",
        }:
            raise StructureError("The weekly period must contain only start_date and end_date.")
        period = WeeklyPeriod(
            start_date=_read_date(period_document["start_date"]),
            end_date=_read_date(period_document["end_date"]),
        )
        contacts = document["contacts"]
        if not isinstance(contacts, list):
            raise StructureError("Weekly contacts must be a JSON list.")
        _validate_metadata(document, len(contacts))
        week = WeeklyContacts(
            period=period,
            contacts=tuple(_decode_contact(contact) for contact in contacts),
        )
        LOGGER.info("Weekly input validated: %s contacts.", len(week.contacts))
        return week


def _decode_contact(input: JsonValue) -> WeeklyContact:
    if not isinstance(input, dict) or not {"raw_name", "last_activity_date"} <= input.keys() <= {
        "raw_name",
        "last_activity_date",
        "phone",
        "is_group",
    }:
        raise StructureError("The contact has missing or unknown fields.")
    name = input["raw_name"]
    phone = input.get("phone")
    is_group = input.get("is_group", False)
    if (
        not isinstance(name, str)
        or not (phone is None or isinstance(phone, str))
        or not isinstance(is_group, bool)
    ):
        raise StructureError("The contact has invalid field types.")
    return WeeklyContact(
        raw_name=name,
        last_activity_date=_read_date(input["last_activity_date"]),
        phone=phone,
        is_group=is_group,
    )


def _read_date(value: JsonValue) -> date:
    if not isinstance(value, str):
        raise StructureError("Weekly dates must be YYYY-MM-DD strings.")
    try:
        parsed_date = date.fromisoformat(value)
    except ValueError as cause:
        raise DomainValidationError("The weekly date is not a valid YYYY-MM-DD date.") from cause
    if parsed_date.isoformat() != value:
        raise DomainValidationError("The weekly date must use exactly the YYYY-MM-DD format.")
    return parsed_date


def _validate_metadata(document: dict[str, JsonValue], count_contacts: int) -> None:
    if "extracted_at" in document:
        instant = document["extracted_at"]
        if not isinstance(instant, str):
            raise StructureError("The extraction timestamp must be an ISO string with a time zone.")
        try:
            date_extraction = datetime.fromisoformat(instant)
        except ValueError as cause:
            raise DomainValidationError(
                "The extraction timestamp is not a valid ISO instant."
            ) from cause
        if date_extraction.utcoffset() is None:
            raise DomainValidationError("The extraction timestamp must be timezone-aware.")
    if "total_chats_scanned" in document:
        total = document["total_chats_scanned"]
        if type(total) is not int:
            raise StructureError("The total scanned chat count must be an integer.")
        if total < count_contacts:
            raise DomainValidationError(
                "The scanned chat count cannot be smaller than the contact count."
            )
