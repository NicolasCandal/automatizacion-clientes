"""Pure activity parsing for WhatsApp text exports."""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from .errors import DomainValidationError, StructureError
from .models import WeeklyContacts, WeeklyPeriod

type DateOrder = Literal["day_month", "month_day"]

_DATE = "(?P<date>[0-9]{1,4}[/.-][0-9]{1,2}[/.-][0-9]{1,4})"
_HOUR = (
    r"(?P<hour>[0-9]{1,2}):(?P<minute>[0-9]{2})(?::(?P<second>[0-9]{2}))?"
    r"(?:\s*(?P<meridiem>[ap]\.?\s*m\.?))?"
)
_ANDROID = re.compile("^" + _DATE + ",?\\s+" + _HOUR + "\\s+-\\s+(?P<remainder>.*)$", re.IGNORECASE)
_IOS = re.compile("^\\[" + _DATE + ",?\\s+" + _HOUR + "\\]\\s+(?P<remainder>.*)$", re.IGNORECASE)
_LOOKS_DATED = re.compile("^\\[?[0-9]{1,4}[/.-][0-9]{1,2}[/.-][0-9]{1,4}(?:,|\\s)")


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatActivity:
    """Recognized activity without message bodies or sender names."""

    last_activity_date: date | None
    recognized_messages: int
    messages_in_period: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ContactExtraction:
    """Weekly input and counters from a fully validated chat batch."""

    week: WeeklyContacts = field(repr=False)
    total_chats_scanned: int
    recognized_messages: int

    def __post_init__(self) -> None:
        """Reject negative or inconsistent extraction metadata."""
        for count in (self.total_chats_scanned, self.recognized_messages):
            if type(count) is not int or count < len(self.week.contacts):
                raise DomainValidationError(
                    "Extraction counts do not correspond to the weekly contacts."
                )


def parse_activity(
    lines: Iterable[str],
    *,
    period: WeeklyPeriod,
    senders: tuple[str, ...],
    date_order: DateOrder,
    short_year_century: int | None = None,
) -> ChatActivity:
    """Find the last activity date in the period without retaining message bodies."""
    if (
        date_order not in {"day_month", "month_day"}
        or not senders
        or len(set(senders)) != len(senders)
        or any(
            not name or name != name.strip() or "\n" in name or "\r" in name or ": " in name
            for name in senders
        )
        or (
            short_year_century is not None
            and (type(short_year_century) is not int or short_year_century not in {1900, 2000})
        )
    ):
        raise DomainValidationError("Configure valid senders, date order, and short-year century.")
    labels = frozenset(senders)
    last: date | None = None
    messages = 0
    included = 0
    headers = 0
    for line in lines:
        text = line.rstrip("\r\n").lstrip("﻿‎‏ ")
        if "\r" in text or "\n" in text:
            raise StructureError("Each input item must contain a single LF or CRLF line.")
        matching = _ANDROID.match(text) or _IOS.match(text)
        if matching is None:
            if _LOOKS_DATED.match(text) or (not headers and text.strip()):
                raise StructureError("The export contains an unsupported header.")
            continue
        headers += 1
        activity_date = _read_date(matching["date"], date_order, short_year_century)
        _validate_hour(matching)
        remainder = matching["remainder"].lstrip("‎‏")
        separator = remainder.find(": ")
        label = (
            remainder[:separator]
            if separator >= 0
            else remainder[:-1]
            if remainder.endswith(":")
            else ""
        )
        if label not in labels:
            continue
        messages += 1
        if period.contains(activity_date):
            included += 1
            last = activity_date if last is None else max(last, activity_date)
    if not headers:
        raise StructureError("The export contains no supported headers.")
    if not messages:
        raise DomainValidationError("No activity from the configured senders was recognized.")
    return ChatActivity(
        last_activity_date=last, recognized_messages=messages, messages_in_period=included
    )


def _read_date(text: str, order: DateOrder, century: int | None) -> date:
    if len(set(re.findall("[/.-]", text))) != 1:
        raise DomainValidationError("The exported date mixes incompatible separators.")
    parts = re.split("[/.-]", text)
    if len(parts[0]) == 4 and text.count("-") == 2:
        year, month, day = (int(part) for part in parts)
    else:
        if len(parts[0]) > 2 or len(parts[1]) > 2 or len(parts[2]) not in {2, 4}:
            raise DomainValidationError("The exported date uses an unsupported format.")
        first, second, year = (int(part) for part in parts)
        day, month = (first, second) if order == "day_month" else (second, first)
        if len(parts[2]) == 2:
            if century is None:
                raise DomainValidationError("Two-digit years require an explicit century.")
            year += century
    try:
        return date(year, month, day)
    except ValueError as cause:
        raise DomainValidationError("The export contains an invalid date.") from cause


def _validate_hour(matching: re.Match[str]) -> None:
    hour, minute = int(matching["hour"]), int(matching["minute"])
    second = int(matching["second"] or "0")
    meridiem = matching["meridiem"]
    if (
        not 0 <= minute <= 59
        or not 0 <= second <= 59
        or not (1 <= hour <= 12 if meridiem else 0 <= hour <= 23)
    ):
        raise DomainValidationError("The export contains an invalid time.")
