"""JSON boundary validation without modifying the original extraction."""

import logging
from datetime import date
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from client_automation.core.contracts import ContactReader
from client_automation.core.errors import DomainValidationError, PersistenceError, StructureError
from client_automation.core.models import WeeklyContact, WeeklyContacts, WeeklyPeriod
from client_automation.infra.json_contact_reader import JsonContactReader
from client_automation.infra.json_storage import JsonValue, save_json


@pytest.fixture
def document() -> dict[str, JsonValue]:
    return {
        "period": {"start_date": "2026-11-02", "end_date": "2026-11-06"},
        "extracted_at": "2026-11-06T18:04:00-03:00",
        "total_chats_scanned": 812,
        "contacts": [
            {
                "raw_name": "  Contact private (IOL)  ",
                "last_activity_date": "2026-11-05",
                "phone": "+5491100000000",
                "is_group": False,
            }
        ],
    }


def test_constructor_no_opens_files_nor_creates_folders(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    path = tmp_path / "new" / "week.json"
    read = mocker.patch.object(Path, "read_text", side_effect=PermissionError("locked"))

    reader: ContactReader = JsonContactReader(path)

    assert reader is not None
    read.assert_not_called()
    assert not path.parent.exists()


def test_format_documented_produces_models_typed_without_modify_file(
    tmp_path: Path, document: dict[str, JsonValue]
) -> None:
    path = tmp_path / "week.json"
    save_json(path, document)
    original = path.read_bytes()
    reader: ContactReader = JsonContactReader(path)

    week = reader.read()

    assert week == WeeklyContacts(
        period=WeeklyPeriod(start_date=date(2026, 11, 2), end_date=date(2026, 11, 6)),
        contacts=(
            WeeklyContact(
                raw_name="  Contact private (IOL)  ",
                last_activity_date=date(2026, 11, 5),
                phone="+5491100000000",
                is_group=False,
            ),
        ),
    )
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


def test_metadata_and_fields_optional_can_omit(tmp_path: Path) -> None:
    path = tmp_path / "week.json"
    save_json(
        path,
        {
            "period": {"start_date": "2026-11-05", "end_date": "2026-11-05"},
            "contacts": [{"raw_name": "Name", "last_activity_date": "2026-11-05"}],
        },
    )

    week = JsonContactReader(path).read()

    assert week.contacts == (WeeklyContact(raw_name="Name", last_activity_date=date(2026, 11, 5)),)


def test_preserves_limits_inclusive_order_groups_and_contacts_repeated(tmp_path: Path) -> None:
    path = tmp_path / "week.json"
    contact: dict[str, JsonValue] = {
        "raw_name": "Group of sample",
        "last_activity_date": "2026-11-06",
        "is_group": True,
    }
    save_json(
        path,
        {
            "period": {"start_date": "2026-11-02", "end_date": "2026-11-06"},
            "contacts": [
                contact,
                {"raw_name": "First in date", "last_activity_date": "2026-11-02"},
                contact,
            ],
            "total_chats_scanned": 3,
        },
    )

    week = JsonContactReader(path).read()

    assert tuple(contact.raw_name for contact in week.contacts) == (
        "Group of sample",
        "First in date",
        "Group of sample",
    )
    assert week.contacts[0].is_group
    assert week.contacts[0] == week.contacts[2]
    assert week.contacts[1].last_activity_date == date(2026, 11, 2)
    assert week.contacts[0].last_activity_date == date(2026, 11, 6)


def test_contact_without_name_preserves_phone_without_infer_country(
    tmp_path: Path, document: dict[str, JsonValue]
) -> None:
    path = tmp_path / "week.json"
    document["contacts"] = [
        {"raw_name": "", "last_activity_date": "2026-11-05", "phone": "00123456"}
    ]
    save_json(path, document)

    week = JsonContactReader(path).read()

    assert week.contacts[0].raw_name == ""
    assert week.contacts[0].phone == "00123456"


def test_week_without_contacts_is_validates(tmp_path: Path, document: dict[str, JsonValue]) -> None:
    path = tmp_path / "week.json"
    document["contacts"] = []
    document["total_chats_scanned"] = 0
    save_json(path, document)

    assert JsonContactReader(path).read().contacts == ()


def test_each_read_reflects_file_current_and_no_reuses_contacts_previous(
    tmp_path: Path, document: dict[str, JsonValue]
) -> None:
    path = tmp_path / "week.json"
    save_json(path, document)
    reader = JsonContactReader(path)
    first = reader.read()
    document["contacts"] = []
    save_json(path, document)

    assert reader.read().contacts == ()
    assert len(first.contacts) == 1
    path.write_bytes(b"{incomplete")
    with pytest.raises(StructureError):
        reader.read()


@pytest.mark.parametrize(
    "content",
    [
        None,
        [],
        {},
        {"contacts": []},
        {"period": {"start_date": "2026-11-02", "end_date": "2026-11-06"}},
        {"period": {}, "contacts": []},
        {"period": [], "contacts": []},
        {"period": {"start_date": "2026-11-02"}, "contacts": []},
        {
            "period": {"start_date": "2026-11-02", "end_date": "2026-11-06", "other": 1},
            "contacts": [],
        },
        {"period": {"start_date": "2026-11-02", "end_date": "2026-11-06"}, "contacts": {}},
        {
            "period": {"start_date": "2026-11-02", "end_date": "2026-11-06"},
            "contacts": [],
            "messages": [],
        },
    ],
)
def test_rejects_schema_weekly_incorrect_without_modify_input(
    tmp_path: Path, content: JsonValue
) -> None:
    path = tmp_path / "week.json"
    save_json(path, content)
    original = path.read_bytes()

    with pytest.raises(StructureError):
        JsonContactReader(path).read()

    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "input",
    [
        None,
        [],
        {},
        {"raw_name": "Name"},
        {"last_activity_date": "2026-11-05"},
        {"raw_name": None, "last_activity_date": "2026-11-05"},
        {"raw_name": 123, "last_activity_date": "2026-11-05"},
        {"raw_name": "Name", "last_activity_date": "2026-11-05", "phone": 123},
        {"raw_name": "Name", "last_activity_date": "2026-11-05", "is_group": "false"},
        {"raw_name": "Name", "last_activity_date": "2026-11-05", "is_group": 0},
        {"raw_name": "Name", "last_activity_date": "2026-11-05", "is_group": None},
        {"raw_name": "Name", "last_activity_date": "2026-11-05", "message": "Private text"},
    ],
)
def test_rejects_contact_with_fields_or_kinds_incorrect(
    tmp_path: Path, document: dict[str, JsonValue], input: JsonValue
) -> None:
    path = tmp_path / "week.json"
    document["contacts"] = [input]
    save_json(path, document)

    with pytest.raises(StructureError):
        JsonContactReader(path).read()


@pytest.mark.parametrize("field", ["start_date", "end_date", "last_activity_date"])
@pytest.mark.parametrize("value", [None, 20261105])
def test_dates_must_be_text_and_no_coerce(tmp_path: Path, field: str, value: JsonValue) -> None:
    path = tmp_path / "week.json"
    period: dict[str, JsonValue] = {"start_date": "2026-11-02", "end_date": "2026-11-06"}
    contact: dict[str, JsonValue] = {"raw_name": "Name", "last_activity_date": "2026-11-05"}
    if field == "last_activity_date":
        contact[field] = value
    else:
        period[field] = value
    save_json(path, {"period": period, "contacts": [contact]})

    with pytest.raises(StructureError):
        JsonContactReader(path).read()


@pytest.mark.parametrize(
    "date",
    [
        "2026-02-30",
        "2026-11-05T12:00:00-03:00",
        "05/11/2026",
        "yesterday",
        "20261105",
        "2026-W45-4",
    ],
)
def test_rejects_dates_invalid_or_different_of_yyyy_mm_dd(
    tmp_path: Path, document: dict[str, JsonValue], date: str
) -> None:
    path = tmp_path / "week.json"
    document["contacts"] = [{"raw_name": "Name", "last_activity_date": date}]
    save_json(path, document)

    with pytest.raises(DomainValidationError):
        JsonContactReader(path).read()


def test_rejects_period_reversed(tmp_path: Path, document: dict[str, JsonValue]) -> None:
    path = tmp_path / "week.json"
    document["period"] = {"start_date": "2026-11-06", "end_date": "2026-11-02"}
    save_json(path, document)

    with pytest.raises(DomainValidationError, match="precede"):
        JsonContactReader(path).read()


@pytest.mark.parametrize("date", ["2026-11-01", "2026-11-07"])
def test_contact_outside_of_period_no_discards_silently(
    tmp_path: Path, document: dict[str, JsonValue], date: str
) -> None:
    path = tmp_path / "week.json"
    document["contacts"] = [
        {"raw_name": "Valid", "last_activity_date": "2026-11-05"},
        {"raw_name": "Outside the period", "last_activity_date": date},
    ]
    save_json(path, document)

    with pytest.raises(DomainValidationError, match="period"):
        JsonContactReader(path).read()


@pytest.mark.parametrize(
    "input",
    [
        {"raw_name": " ", "last_activity_date": "2026-11-05"},
        {"raw_name": "Name", "last_activity_date": "2026-11-05", "phone": " "},
    ],
)
def test_rejects_identities_empty(
    tmp_path: Path, document: dict[str, JsonValue], input: JsonValue
) -> None:
    path = tmp_path / "week.json"
    document["contacts"] = [input]
    save_json(path, document)

    with pytest.raises(DomainValidationError):
        JsonContactReader(path).read()


@pytest.mark.parametrize("instant", [None, 123, False])
def test_metadata_of_extraction_must_be_text(
    tmp_path: Path, document: dict[str, JsonValue], instant: JsonValue
) -> None:
    path = tmp_path / "week.json"
    document["extracted_at"] = instant
    save_json(path, document)

    with pytest.raises(StructureError):
        JsonContactReader(path).read()


@pytest.mark.parametrize("instant", ["no is one date", "2026-11-05", "2026-11-05T12:00:00"])
def test_metadata_of_extraction_requires_instant_with_zone(
    tmp_path: Path, document: dict[str, JsonValue], instant: str
) -> None:
    path = tmp_path / "week.json"
    document["extracted_at"] = instant
    save_json(path, document)

    with pytest.raises(DomainValidationError):
        JsonContactReader(path).read()


@pytest.mark.parametrize("instant", ["2026-11-06T21:04:00Z", "2026-11-06T18:04:00.123-03:00"])
def test_metadata_with_zone_is_valid_without_modify_dates_of_chat(
    tmp_path: Path, document: dict[str, JsonValue], instant: str
) -> None:
    path = tmp_path / "week.json"
    document["extracted_at"] = instant
    save_json(path, document)

    assert JsonContactReader(path).read().contacts[0].last_activity_date == date(2026, 11, 5)


@pytest.mark.parametrize("total", [None, True, 1.0, "812"])
def test_total_scanned_requires_integer_strict(
    tmp_path: Path, document: dict[str, JsonValue], total: JsonValue
) -> None:
    path = tmp_path / "week.json"
    document["total_chats_scanned"] = total
    save_json(path, document)

    with pytest.raises(StructureError):
        JsonContactReader(path).read()


@pytest.mark.parametrize("total", [-1, 0])
def test_total_scanned_no_can_be_less_that_contacts(
    tmp_path: Path, document: dict[str, JsonValue], total: int
) -> None:
    path = tmp_path / "week.json"
    document["total_chats_scanned"] = total
    save_json(path, document)

    with pytest.raises(DomainValidationError):
        JsonContactReader(path).read()


def test_file_missing_no_interprets_as_week_empty(tmp_path: Path) -> None:
    path = tmp_path / "new" / "week.json"

    with pytest.raises(PersistenceError) as captured:
        JsonContactReader(path).read()

    assert isinstance(captured.value.__cause__, FileNotFoundError)
    assert not path.parent.exists()


def test_error_of_access_translates_without_include_details_private(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    cause = PermissionError("Path private")
    mocker.patch.object(Path, "read_text", side_effect=cause)

    with pytest.raises(PersistenceError) as captured:
        JsonContactReader(tmp_path / "week.json").read()

    assert captured.value.__cause__ is cause
    assert "private" not in str(captured.value)


def test_log_of_success_only_includes_count_without_identities(
    tmp_path: Path, document: dict[str, JsonValue], caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "contact-private.json"
    save_json(path, document)

    with caplog.at_level(logging.INFO):
        JsonContactReader(path).read()

    assert len(caplog.records) == 1
    assert "1 contacts" in caplog.text
    assert "private" not in caplog.text
    assert "+5491100000000" not in caplog.text


def test_error_of_validation_no_registers_contacts_partial(
    tmp_path: Path, document: dict[str, JsonValue], caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "week.json"
    document["contacts"] = [
        {"raw_name": "Name private", "last_activity_date": "2026-11-05"},
        {"raw_name": "Name private", "last_activity_date": "2026-11-07"},
    ]
    save_json(path, document)

    with caplog.at_level(logging.INFO), pytest.raises(DomainValidationError) as captured:
        JsonContactReader(path).read()

    assert not caplog.records
    assert "private" not in str(captured.value)
