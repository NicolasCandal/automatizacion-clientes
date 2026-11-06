"""Versioned, validated persistence of weekly matching results."""

import logging
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Final

from client_automation.core.errors import DomainValidationError, StructureError
from client_automation.core.models import (
    ClientLocation,
    IgnoredContact,
    IgnoredReason,
    MatchCandidate,
    MatchMethod,
    PendingContact,
    PendingReason,
    ResolvedContact,
    WeeklyContact,
    WeeklyResolution,
)
from client_automation.infra.json_storage import JsonValue, read_json, save_json

LOGGER = logging.getLogger(__name__)
RESOLUTION_SCHEMA_VERSION: Final[int] = 1


class JsonResolutionStore:
    """Implement ResolutionStore while preserving the complete report."""

    def __init__(self, path: Path) -> None:
        """Accept the output path without disk access or directory creation."""
        self._path = path

    def read(self) -> WeeklyResolution:
        """Validate every result before returning the report."""
        document = read_json(self._path)
        if not isinstance(document, dict) or "schema_version" not in document:
            raise StructureError(
                "The report has no schema version. Generate it with the current service."
            )
        version = document["schema_version"]
        if type(version) is not int or version != RESOLUTION_SCHEMA_VERSION:
            raise StructureError("The report schema version is unsupported.")
        input = _read_object(
            document, {"schema_version", "resolved", "pending", "ignored", "warnings"}
        )
        resolution = WeeklyResolution(
            resolved=tuple(_decode_resolved(value) for value in _read_list(input["resolved"])),
            pending=tuple(_decode_pending(value) for value in _read_list(input["pending"])),
            ignored=tuple(_decode_ignored(value) for value in _read_list(input["ignored"])),
            warnings=tuple(_read_text(value) for value in _read_list(input["warnings"])),
        )
        _register_summary("read", resolution)
        return resolution

    def save(self, resolution: WeeklyResolution) -> None:
        """Save all results through atomic replacement."""
        resolved: list[JsonValue] = [_encode_resolved(value) for value in resolution.resolved]
        pending: list[JsonValue] = [_encode_pending(value) for value in resolution.pending]
        ignored: list[JsonValue] = [
            {
                "kind": value.kind,
                "contact": _encode_contact(value.contact),
                "reason": value.reason.value,
            }
            for value in resolution.ignored
        ]
        warnings: list[JsonValue] = list(resolution.warnings)
        save_json(
            self._path,
            {
                "schema_version": RESOLUTION_SCHEMA_VERSION,
                "resolved": resolved,
                "pending": pending,
                "ignored": ignored,
                "warnings": warnings,
            },
        )
        _register_summary("saved", resolution)


def _decode_resolved(document: JsonValue) -> ResolvedContact:
    input = _read_result(
        document,
        {"kind", "contact", "person_id", "locations", "score", "method"},
        "resolved",
    )
    return ResolvedContact(
        contact=_decode_contact(input["contact"]),
        person_id=_read_text(input["person_id"]),
        locations=tuple(_decode_location(value) for value in _read_list(input["locations"])),
        score=_read_score(input["score"]),
        method=_read_enum(input["method"], MatchMethod),
    )


def _decode_pending(document: JsonValue) -> PendingContact:
    input = _read_result(document, {"kind", "contact", "reason", "candidates"}, "pending")
    return PendingContact(
        contact=_decode_contact(input["contact"]),
        reason=_read_enum(input["reason"], PendingReason),
        candidates=tuple(_decode_candidate(value) for value in _read_list(input["candidates"])),
    )


def _decode_ignored(document: JsonValue) -> IgnoredContact:
    input = _read_result(document, {"kind", "contact", "reason"}, "ignored")
    return IgnoredContact(
        contact=_decode_contact(input["contact"]),
        reason=_read_enum(input["reason"], IgnoredReason),
    )


def _decode_contact(document: JsonValue) -> WeeklyContact:
    input = _read_object(document, {"raw_name", "last_activity_date", "phone", "is_group"})
    phone = input["phone"]
    is_group = input["is_group"]
    if not (phone is None or isinstance(phone, str)) or not isinstance(is_group, bool):
        raise StructureError("The report's phone or group flag has an invalid type.")
    text_date = _read_text(input["last_activity_date"])
    try:
        last_activity_date = date.fromisoformat(text_date)
    except ValueError as cause:
        raise DomainValidationError("The report contact date is invalid.") from cause
    if last_activity_date.isoformat() != text_date:
        raise DomainValidationError("Report contact dates must use YYYY-MM-DD.")
    return WeeklyContact(
        raw_name=_read_text(input["raw_name"]),
        last_activity_date=last_activity_date,
        phone=phone,
        is_group=is_group,
    )


def _decode_location(document: JsonValue) -> ClientLocation:
    input = _read_object(document, {"sheet", "row"})
    row = input["row"]
    if type(row) is not int:
        raise StructureError("A report destination row must be an integer.")
    return ClientLocation(sheet=_read_text(input["sheet"]), row=row)


def _decode_candidate(document: JsonValue) -> MatchCandidate:
    input = _read_object(document, {"person_id", "canonical_name", "sheets", "score", "method"})
    sheets = tuple(_read_text(value) for value in _read_list(input["sheets"]))
    if len(sheets) != len(set(sheets)):
        raise StructureError("A report candidate's sheets cannot repeat.")
    return MatchCandidate(
        person_id=_read_text(input["person_id"]),
        canonical_name=_read_text(input["canonical_name"]),
        sheets=frozenset(sheets),
        score=_read_score(input["score"]),
        method=_read_enum(input["method"], MatchMethod),
    )


def _encode_contact(contact: WeeklyContact) -> dict[str, JsonValue]:
    return {
        "raw_name": contact.raw_name,
        "last_activity_date": contact.last_activity_date.isoformat(),
        "phone": contact.phone,
        "is_group": contact.is_group,
    }


def _encode_resolved(result: ResolvedContact) -> dict[str, JsonValue]:
    locations: list[JsonValue] = [
        {"sheet": location.sheet, "row": location.row} for location in result.locations
    ]
    return {
        "kind": result.kind,
        "contact": _encode_contact(result.contact),
        "person_id": result.person_id,
        "locations": locations,
        "score": result.score,
        "method": result.method.value,
    }


def _encode_pending(result: PendingContact) -> dict[str, JsonValue]:
    candidates: list[JsonValue] = []
    for candidate in result.candidates:
        sheets: list[JsonValue] = list(sorted(candidate.sheets))
        candidates.append(
            {
                "person_id": candidate.person_id,
                "canonical_name": candidate.canonical_name,
                "sheets": sheets,
                "score": candidate.score,
                "method": candidate.method.value,
            }
        )
    return {
        "kind": result.kind,
        "contact": _encode_contact(result.contact),
        "reason": result.reason.value,
        "candidates": candidates,
    }


def _read_object(value: JsonValue, fields: set[str]) -> dict[str, JsonValue]:
    if not isinstance(value, dict) or value.keys() != fields:
        raise StructureError("Report objects have missing or unknown fields.")
    return value


def _read_result(value: JsonValue, fields: set[str], kind: str) -> dict[str, JsonValue]:
    input = _read_object(value, fields)
    if input["kind"] != kind:
        raise StructureError("A result type does not match its report collection.")
    return input


def _read_list(value: JsonValue) -> list[JsonValue]:
    if not isinstance(value, list):
        raise StructureError("A report list field has an incorrect type.")
    return value


def _read_text(value: JsonValue) -> str:
    if not isinstance(value, str):
        raise StructureError("A report text field has an incorrect type.")
    return value


def _read_score(value: JsonValue) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise StructureError("Report scores must be numbers without type coercion.")
    try:
        return float(value)
    except OverflowError as cause:
        raise DomainValidationError("The report score exceeds the permitted range.") from cause


def _read_enum[EnumType: StrEnum](value: JsonValue, kind_enum: type[EnumType]) -> EnumType:
    try:
        return kind_enum(_read_text(value))
    except ValueError as cause:
        raise StructureError("The report contains an unknown match method or reason.") from cause


def _register_summary(action: str, resolution: WeeklyResolution) -> None:
    LOGGER.info(
        "Report %s: %s resolved, %s pending, %s ignored, and %s warnings.",
        action,
        len(resolution.resolved),
        len(resolution.pending),
        len(resolution.ignored),
        len(resolution.warnings),
    )
