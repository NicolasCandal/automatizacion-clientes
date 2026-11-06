"""Pure parsing of synthetic headers and inclusive date filtering."""

from datetime import date
from typing import cast

import pytest

from client_automation.core.errors import DomainValidationError, StructureError
from client_automation.core.models import WeeklyContacts, WeeklyPeriod
from client_automation.core.whatsapp_import import (
    ContactExtraction,
    DateOrder,
    parse_activity,
)

PERIOD = WeeklyPeriod(start_date=date(2026, 11, 2), end_date=date(2026, 11, 6))


@pytest.mark.parametrize(
    "header",
    [
        "02/11/2026, 09:15 - Axa: content private",
        "2/11/2026 09:15 - Axa: content private",
        "[02/11/2026, 09:15:31] Axa: content private",
        "[02/11/2026, 9:15 PM] Axa: content private",
        "02/11/2026, 9:15 p. m. - Axa: content private",
        "[02/11/2026, 9:15:00\u202fp.\u00a0m.] Axa: content private",
        "02.11.2026, 00:00 - Axa:",
        "02-11-2026, 23:59:59 - Axa: <Multimedia omitted>",
        "\ufeff\u200e[2026-11-02, 12:00 AM] \u200eAxa: content private\r\n",
    ],
)
def test_headers_supported_no_retain_body(header: str) -> None:
    result = parse_activity([header], period=PERIOD, senders=("Axa",), date_order="day_month")
    assert result.last_activity_date == date(2026, 11, 2)
    assert result.recognized_messages == result.messages_in_period == 1
    assert "private" not in repr(result)
    assert "Axa" not in repr(result)


def test_last_date_computes_within_of_period_no_of_chat_complete() -> None:
    result = parse_activity(
        [
            "01/11/2026, 12:00 - Axa: previous",
            "06/11/2026, 12:00 - Me: within",
            "03/11/2026, 12:00 - Axa: outside of order",
            "20/11/2026, 12:00 - Axa: later",
        ],
        period=PERIOD,
        senders=("Axa", "Me"),
        date_order="day_month",
    )
    assert result.last_activity_date == date(2026, 11, 6)
    assert result.recognized_messages == 4
    assert result.messages_in_period == 2


def test_warnings_senders_no_selected_and_multiline_no_count() -> None:
    result = parse_activity(
        [
            "\n",
            "02/11/2026, 12:00 - Messages are end-to-end encrypted.",
            "02/11/2026, 12:00 - Axa: first line",
            "continuation: no new message",
            "\n",
            "05/11/2026, 12:00 - Other: no selected",
            "06/11/2026, 12:00 - Description changed: system text",
        ],
        period=PERIOD,
        senders=("Axa",),
        date_order="day_month",
    )
    assert result.last_activity_date == date(2026, 11, 2)
    assert result.recognized_messages == 1


def test_all_outside_of_period_returns_date_null() -> None:
    result = parse_activity(
        ["01/11/2026, 12:00 - Axa: previous"],
        period=PERIOD,
        senders=("Axa",),
        date_order="day_month",
    )
    assert result.last_activity_date is None
    assert result.messages_in_period == 0
    assert result.recognized_messages == 1


def test_order_month_day_and_century_are_explicit() -> None:
    result = parse_activity(
        ["11/02/26, 12:00 - Axa: text"],
        period=PERIOD,
        senders=("Axa",),
        date_order="month_day",
        short_year_century=2000,
    )
    assert result.last_activity_date == date(2026, 11, 2)
    legacy = parse_activity(
        ["11/02/26, 12:00 - Axa: text"],
        period=PERIOD,
        senders=("Axa",),
        date_order="month_day",
        short_year_century=1900,
    )
    assert legacy.last_activity_date is None


@pytest.mark.parametrize(
    "header",
    [
        "31/11/2026, 12:00 - Axa: secret",
        "02/13/2026, 12:00 - Axa: secret",
        "02/11/26, 12:00 - Axa: secret",
        "02/11/026, 12:00 - Axa: secret",
        "02/11-2026, 12:00 - Axa: secret",
        "2026/11/02, 12:00 - Axa: secret",
        "02/11/2026, 24:00 - Axa: secret",
        "02/11/2026, 10:60 - Axa: secret",
        "02/11/2026, 10:10:60 - Axa: secret",
        "02/11/2026, 00:10 AM - Axa: secret",
        "02/11/2026, 13:10 PM - Axa: secret",
    ],
)
def test_dates_hours_and_years_ambiguous_reject(header: str) -> None:
    with pytest.raises(DomainValidationError) as error:
        parse_activity([header], period=PERIOD, senders=("Axa",), date_order="day_month")
    assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    "lines",
    [
        [],
        ["\n"],
        ["file unrelated"],
        ["02/11/2026, xx:00 - Axa: secret"],
        ["02/11/2026, 12:00 - Axa: valid", "03/11/2026, 12:xx - Axa: secret"],
        ["02/11/2026, 12:00 - Axa: text\r03/11/2026, 12:00 - Axa: other"],
    ],
)
def test_exports_empty_or_incompatible_no_generate_partial(lines: list[str]) -> None:
    with pytest.raises(StructureError):
        parse_activity(lines, period=PERIOD, senders=("Axa",), date_order="day_month")


@pytest.mark.parametrize(
    "senders", [(), ("",), (" Axa",), ("Axa", "Axa"), ("Axa\nMe",), ("Axa: Company",)]
)
def test_senders_invalid_reject(senders: tuple[str, ...]) -> None:
    with pytest.raises(DomainValidationError):
        parse_activity([], period=PERIOD, senders=senders, date_order="day_month")


@pytest.mark.parametrize("century", [1800, 2020, True])
def test_century_no_supported_rejects(century: int) -> None:
    with pytest.raises(DomainValidationError):
        parse_activity(
            [], period=PERIOD, senders=("Axa",), date_order="day_month", short_year_century=century
        )


def test_no_accepts_order_invented_or_senders_without_activity() -> None:
    with pytest.raises(DomainValidationError):
        parse_activity([], period=PERIOD, senders=("Axa",), date_order=cast(DateOrder, "automatic"))
    with pytest.raises(DomainValidationError):
        parse_activity(
            ["02/11/2026, 12:00 - Other: text"],
            period=PERIOD,
            senders=("Axa",),
            date_order="day_month",
        )


@pytest.mark.parametrize("count", [-1, True])
def test_counts_of_extraction_are_integers_no_negative(count: int) -> None:
    with pytest.raises(DomainValidationError):
        ContactExtraction(
            week=WeeklyContacts(period=PERIOD, contacts=()),
            total_chats_scanned=count,
            recognized_messages=0,
        )
