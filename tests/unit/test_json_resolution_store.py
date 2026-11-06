"""Report integrity, result validation, and safe persistence."""

import logging
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from client_automation.core.contracts import ResolutionStore
from client_automation.core.errors import DomainValidationError, PersistenceError, StructureError
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
from client_automation.infra.json_resolution_store import JsonResolutionStore
from client_automation.infra.json_storage import JsonValue, read_json, save_json


@pytest.fixture
def contact_document() -> dict[str, JsonValue]:
    return {
        "raw_name": "  Name private (IOL)  ",
        "last_activity_date": "2026-11-05",
        "phone": "+5491100000000",
        "is_group": False,
    }


@pytest.fixture
def location_document() -> dict[str, JsonValue]:
    return {"sheet": "IOL", "row": 3}


@pytest.fixture
def candidate_document() -> dict[str, JsonValue]:
    return {
        "person_id": "p0001",
        "canonical_name": "Name private",
        "sheets": ["IOL", "Bull"],
        "score": 92.3076923076923,
        "method": "fuzzy",
    }


@pytest.fixture
def resolved_document(
    contact_document: dict[str, JsonValue], location_document: dict[str, JsonValue]
) -> dict[str, JsonValue]:
    return {
        "kind": "resolved",
        "contact": contact_document,
        "person_id": "p0001",
        "locations": [location_document, {"sheet": "IOL", "row": 20}],
        "score": 100,
        "method": "phone",
    }


@pytest.fixture
def pending_document(
    contact_document: dict[str, JsonValue], candidate_document: dict[str, JsonValue]
) -> dict[str, JsonValue]:
    return {
        "kind": "pending",
        "contact": contact_document,
        "reason": "ambiguous",
        "candidates": [candidate_document],
    }


@pytest.fixture
def ignored_document(contact_document: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {"kind": "ignored", "contact": contact_document, "reason": "excluded"}


@pytest.fixture
def document(
    resolved_document: dict[str, JsonValue],
    pending_document: dict[str, JsonValue],
    ignored_document: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    return {
        "schema_version": 1,
        "resolved": [resolved_document],
        "pending": [pending_document],
        "ignored": [ignored_document],
        "warnings": ["Warning private", "Warning private", ""],
    }


@pytest.fixture
def resolution() -> WeeklyResolution:
    contact = WeeklyContact(
        raw_name="  Name private (IOL)  ",
        last_activity_date=date(2026, 11, 5),
        phone="+5491100000000",
    )
    resolved = ResolvedContact(
        contact=contact,
        person_id="p0042",
        locations=(ClientLocation(sheet="IOL", row=3), ClientLocation(sheet="IOL", row=20)),
        score=100.0,
        method=MatchMethod.PHONE,
    )
    candidate = MatchCandidate(
        person_id="p0007",
        canonical_name="Candidate private",
        sheets=frozenset({"IOL", "Bull"}),
        score=85.71428571428571,
        method=MatchMethod.FUZZY,
    )
    return WeeklyResolution(
        resolved=(resolved, replace(resolved, person_id="p0008")),
        pending=(
            PendingContact(
                contact=contact, reason=PendingReason.AMBIGUOUS, candidates=(candidate,)
            ),
            PendingContact(
                contact=replace(contact, raw_name="Other"), reason=PendingReason.NO_MATCH
            ),
        ),
        ignored=(
            IgnoredContact(contact=replace(contact, is_group=True), reason=IgnoredReason.GROUP),
            IgnoredContact(contact=contact, reason=IgnoredReason.EXCLUDED),
        ),
        warnings=("Warning private", "Warning private", ""),
    )


def test_constructor_no_accesses_on_disk(tmp_path: Path, mocker: MockerFixture) -> None:
    path = tmp_path / "new" / "report.json"
    read = mocker.patch.object(Path, "read_text", side_effect=PermissionError("locked"))

    store: ResolutionStore = JsonResolutionStore(path)

    assert store is not None
    read.assert_not_called()
    assert not path.parent.exists()


def test_save_and_read_preserve_report_complete_and_holders(
    tmp_path: Path, resolution: WeeklyResolution
) -> None:
    path = tmp_path / "new" / "report.json"
    store: ResolutionStore = JsonResolutionStore(path)

    store.save(resolution)
    reloaded = store.read()

    assert reloaded == resolution
    assert reloaded.resolved[0].contact == reloaded.resolved[1].contact
    assert reloaded.resolved[0].person_id != reloaded.resolved[1].person_id
    assert reloaded.resolved[0].locations == resolution.resolved[0].locations
    assert reloaded.pending[0].candidates[0].score == 85.71428571428571
    assert reloaded.pending[1].candidates == ()
    assert reloaded.warnings == ("Warning private", "Warning private", "")
    assert list(path.parent.iterdir()) == [path]
    original = path.read_bytes()
    store.save(reloaded)
    assert path.read_bytes() == original


def test_reads_document_independent_without_modify(
    tmp_path: Path, document: dict[str, JsonValue]
) -> None:
    path = tmp_path / "report.json"
    save_json(path, document)
    original = path.read_bytes()

    resolution = JsonResolutionStore(path).read()

    assert resolution.resolved[0].method is MatchMethod.PHONE
    assert resolution.resolved[0].contact.last_activity_date == date(2026, 11, 5)
    assert isinstance(resolution.resolved[0].score, float)
    assert resolution.pending[0].reason is PendingReason.AMBIGUOUS
    assert resolution.pending[0].candidates[0].sheets == frozenset({"IOL", "Bull"})
    assert resolution.ignored[0].reason is IgnoredReason.EXCLUDED
    assert path.read_bytes() == original


def test_report_empty_is_valid_and_saves_with_all_the_collections(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    store = JsonResolutionStore(path)
    store.save(WeeklyResolution())

    assert store.read() == WeeklyResolution()
    assert read_json(path) == {
        "schema_version": 1,
        "resolved": [],
        "pending": [],
        "ignored": [],
        "warnings": [],
    }


@pytest.mark.parametrize("method", list(MatchMethod))
def test_preserves_all_the_methods(
    tmp_path: Path, resolution: WeeklyResolution, method: MatchMethod
) -> None:
    result = replace(resolution.resolved[0], method=method)
    report = replace(resolution, resolved=(result,))
    store = JsonResolutionStore(tmp_path / "report.json")
    store.save(report)

    assert store.read() == report


@pytest.mark.parametrize("reason", list(PendingReason))
def test_preserves_all_the_reasons_pending(
    tmp_path: Path, resolution: WeeklyResolution, reason: PendingReason
) -> None:
    report = replace(resolution, pending=(replace(resolution.pending[0], reason=reason),))
    store = JsonResolutionStore(tmp_path / "report.json")
    store.save(report)

    assert store.read() == report


def test_contact_identified_only_by_phone_preserves(
    tmp_path: Path, resolution: WeeklyResolution
) -> None:
    result = resolution.resolved[0]
    result = replace(result, contact=replace(result.contact, raw_name=""))
    report = WeeklyResolution(resolved=(result,))
    store = JsonResolutionStore(tmp_path / "report.json")
    store.save(report)

    assert store.read() == report


@pytest.mark.parametrize("content", [None, [], {}])
def test_content_incorrect_no_equals_to_report_empty(tmp_path: Path, content: JsonValue) -> None:
    path = tmp_path / "report.json"
    save_json(path, content)

    with pytest.raises(StructureError):
        JsonResolutionStore(path).read()


def test_report_previous_requires_regeneration_without_transform(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    save_json(path, {"resolved": [], "pending": [], "ignored": []})
    original = path.read_bytes()

    with pytest.raises(StructureError, match="version"):
        JsonResolutionStore(path).read()

    assert path.read_bytes() == original


@pytest.mark.parametrize("version", [True, 1.0, "1", None, 0, 2])
def test_version_requires_integer_exact_supported(
    tmp_path: Path, document: dict[str, JsonValue], version: JsonValue
) -> None:
    document["schema_version"] = version
    path = tmp_path / "report.json"
    save_json(path, document)

    with pytest.raises(StructureError):
        JsonResolutionStore(path).read()


@pytest.mark.parametrize("field", ["resolved", "pending", "ignored", "warnings"])
def test_collections_required_without_coercion(
    tmp_path: Path, document: dict[str, JsonValue], field: str
) -> None:
    document[field] = {}
    path = tmp_path / "report.json"
    save_json(path, document)

    with pytest.raises(StructureError):
        JsonResolutionStore(path).read()


@pytest.mark.parametrize("change", ["field_missing", "field_unknown", "kind_incorrect"])
def test_validates_state_and_fields_of_the_results(
    tmp_path: Path,
    document: dict[str, JsonValue],
    resolved_document: dict[str, JsonValue],
    change: str,
) -> None:
    if change == "field_missing":
        del resolved_document["person_id"]
    elif change == "field_unknown":
        resolved_document["messages"] = []
    else:
        resolved_document["kind"] = "pending"
    path = tmp_path / "report.json"
    save_json(path, document)

    with pytest.raises(StructureError):
        JsonResolutionStore(path).read()


@pytest.mark.parametrize("field", ["method", "score"])
@pytest.mark.parametrize("value", [None, True, "unknown"])
def test_rejects_enum_or_score_incompatible(
    tmp_path: Path,
    document: dict[str, JsonValue],
    resolved_document: dict[str, JsonValue],
    field: str,
    value: JsonValue,
) -> None:
    resolved_document[field] = value
    path = tmp_path / "report.json"
    save_json(path, document)

    with pytest.raises(StructureError):
        JsonResolutionStore(path).read()


@pytest.mark.parametrize("score", [-1, 101, 10**400])
def test_rejects_scores_outside_of_scale_without_overflows_without_translate(
    tmp_path: Path,
    document: dict[str, JsonValue],
    resolved_document: dict[str, JsonValue],
    score: int,
) -> None:
    resolved_document["score"] = score
    path = tmp_path / "report.json"
    save_json(path, document)

    with pytest.raises(DomainValidationError):
        JsonResolutionStore(path).read()


@pytest.mark.parametrize("date", ["2026-02-30", "20261105", "2026-11-05T12:00:00"])
def test_date_of_contact_is_of_calendar_and_canonical(
    tmp_path: Path,
    document: dict[str, JsonValue],
    contact_document: dict[str, JsonValue],
    date: str,
) -> None:
    contact_document["last_activity_date"] = date
    path = tmp_path / "report.json"
    save_json(path, document)

    with pytest.raises(DomainValidationError):
        JsonResolutionStore(path).read()


@pytest.mark.parametrize(
    ("field", "value"), [("phone", 123), ("is_group", "false"), ("raw_name", 123)]
)
def test_contact_requires_kinds_strict(
    tmp_path: Path,
    document: dict[str, JsonValue],
    contact_document: dict[str, JsonValue],
    field: str,
    value: JsonValue,
) -> None:
    contact_document[field] = value
    path = tmp_path / "report.json"
    save_json(path, document)

    with pytest.raises(StructureError):
        JsonResolutionStore(path).read()


@pytest.mark.parametrize("row", [True, "3", 3.0])
def test_rows_require_integers_strict(
    tmp_path: Path,
    document: dict[str, JsonValue],
    location_document: dict[str, JsonValue],
    row: JsonValue,
) -> None:
    location_document["row"] = row
    path = tmp_path / "report.json"
    save_json(path, document)

    with pytest.raises(StructureError):
        JsonResolutionStore(path).read()


@pytest.mark.parametrize("duplicate", ["location", "candidate", "sheet"])
def test_rejects_duplicates_that_change_the_meaning_of_result(
    tmp_path: Path,
    document: dict[str, JsonValue],
    resolved_document: dict[str, JsonValue],
    location_document: dict[str, JsonValue],
    pending_document: dict[str, JsonValue],
    candidate_document: dict[str, JsonValue],
    duplicate: str,
) -> None:
    if duplicate == "location":
        resolved_document["locations"] = [location_document, location_document]
    elif duplicate == "candidate":
        pending_document["candidates"] = [candidate_document, candidate_document]
    else:
        candidate_document["sheets"] = ["IOL", "IOL"]
    path = tmp_path / "report.json"
    save_json(path, document)

    with pytest.raises(StructureError if duplicate == "sheet" else DomainValidationError):
        JsonResolutionStore(path).read()


def test_file_missing_is_error_and_no_creates_output(tmp_path: Path) -> None:
    path = tmp_path / "new" / "report.json"

    with pytest.raises(PersistenceError) as captured:
        JsonResolutionStore(path).read()

    assert isinstance(captured.value.__cause__, FileNotFoundError)
    assert not path.parent.exists()


def test_each_read_reflects_file_current_and_rejects_corruption(
    tmp_path: Path, resolution: WeeklyResolution
) -> None:
    path = tmp_path / "report.json"
    store = JsonResolutionStore(path)
    store.save(resolution)
    first = store.read()
    store.save(WeeklyResolution())
    assert store.read() == WeeklyResolution()
    assert first == resolution
    path.write_bytes(b"{incomplete")

    with pytest.raises(StructureError):
        store.read()

    assert path.read_bytes() == b"{incomplete"


def test_failure_of_access_preserves_cause_technical(tmp_path: Path, mocker: MockerFixture) -> None:
    cause = PermissionError("Detail private")
    mocker.patch.object(Path, "read_text", side_effect=cause)

    with pytest.raises(PersistenceError) as captured:
        JsonResolutionStore(tmp_path / "report.json").read()

    assert captured.value.__cause__ is cause
    assert "private" not in str(captured.value)


@pytest.mark.parametrize("previous_file", [True, False])
def test_failure_of_saved_preserves_report_and_allows_retry(
    tmp_path: Path, resolution: WeeklyResolution, mocker: MockerFixture, previous_file: bool
) -> None:
    path = tmp_path / "report.json"
    store = JsonResolutionStore(path)
    if previous_file:
        store.save(resolution)
    updated = replace(resolution, warnings=("New warning",))
    failure = mocker.patch.object(Path, "replace", side_effect=PermissionError("locked"))

    with pytest.raises(PersistenceError):
        store.save(updated)

    if previous_file:
        assert store.read() == resolution
        assert list(tmp_path.iterdir()) == [path]
    else:
        assert list(tmp_path.iterdir()) == []
    mocker.stop(failure)
    store.save(updated)
    assert store.read() == updated


def test_logs_no_reveal_warnings_paths_identities_nor_phones(
    tmp_path: Path, resolution: WeeklyResolution, caplog: pytest.LogCaptureFixture
) -> None:
    store = JsonResolutionStore(tmp_path / "report-private.json")

    with caplog.at_level(logging.INFO):
        store.save(resolution)
        store.read()

    assert len(caplog.records) == 2
    assert "2 resolved, 2 pending, 2 ignored, and 3 warnings" in caplog.text
    for content in ("private", "+5491100000000", "p0042", "p0007"):
        assert content not in caplog.text


def test_failure_of_validation_no_registers_report_partial(
    tmp_path: Path,
    document: dict[str, JsonValue],
    ignored_document: dict[str, JsonValue],
    caplog: pytest.LogCaptureFixture,
) -> None:
    ignored_document["reason"] = "Reason private unknown"
    path = tmp_path / "report.json"
    save_json(path, document)

    with caplog.at_level(logging.INFO), pytest.raises(StructureError) as captured:
        JsonResolutionStore(path).read()

    assert not caplog.records
    assert "private" not in str(captured.value)
