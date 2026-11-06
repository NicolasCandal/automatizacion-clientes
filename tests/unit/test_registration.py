"""Complete plans, dry runs, preconditions, and registration failure recovery."""

from collections.abc import Mapping
from dataclasses import FrozenInstanceError, dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from client_automation.core.catalog import build_catalog
from client_automation.core.errors import (
    AutomationError,
    ConcurrencyError,
    ConfigurationError,
    DomainValidationError,
    OperationTimeoutError,
    PersistenceError,
    RegistrationInterruptedError,
)
from client_automation.core.models import (
    ClientCatalog,
    ClientLocation,
    ClientOccurrence,
    ClientRow,
    IgnoredContact,
    IgnoredReason,
    MatchMethod,
    PendingContact,
    PendingReason,
    ResolvedContact,
    SheetInfo,
    WeeklyContact,
    WeeklyContacts,
    WeeklyPeriod,
    WeeklyResolution,
)
from client_automation.core.registration import (
    Headers,
    RegistrationPlan,
    RegistrationResult,
    SheetMarks,
    build_operations,
    catalog_clients,
)
from client_automation.services.registration import RegistrationService


class InMemorySpreadsheet:
    def __init__(self) -> None:
        self.number_revision = 0
        self.sheets: tuple[SheetInfo, ...] = (
            SheetInfo(
                name="IOL",
                sheet_id=0,
                name_column=1,
                first_week_column=3,
                last_week_column=5,
                last_client_row=8,
            ),
            SheetInfo(
                name="Balanz",
                sheet_id=1,
                name_column=1,
                first_week_column=None,
                last_week_column=None,
                last_client_row=3,
            ),
            SheetInfo(
                name="PPI",
                sheet_id=2,
                name_column=1,
                first_week_column=None,
                last_week_column=None,
                last_client_row=2,
            ),
        )
        self.clients = {
            "IOL": (
                ClientRow(row=3, raw_name="Axa Sample"),
                ClientRow(row=5, raw_name="Notuka Example"),
                ClientRow(row=8, raw_name="Sample, Axa"),
            ),
            "Balanz": (ClientRow(row=3, raw_name="Axa Sample"),),
            "PPI": (),
        }
        self.headers: dict[str, Headers] = {
            "IOL": ((3, " Current week "), (5, "Later")),
            "Balanz": (),
            "PPI": (),
        }
        self.marks = {("IOL", 3): frozenset({5}), ("IOL", 5): frozenset({3})}
        self.backups = 0
        self.writes: list[tuple[str, int, str, frozenset[int], str]] = []

    def revision(self) -> str:
        return f"revision_{self.number_revision}"

    def list_sheets(self) -> tuple[SheetInfo, ...]:
        return self.sheets

    def read_clients(self, sheet: str) -> tuple[ClientRow, ...]:
        return self.clients[sheet]

    def read_headers(self, sheet: str) -> Headers:
        return self.headers[sheet]

    def read_marks(self, sheet: str, column: int) -> frozenset[int]:
        return self.marks.get((sheet, column), frozenset())

    def last_week(self, sheet: str) -> tuple[int, str]:
        return self.headers[sheet][-1] if self.headers[sheet] else (1, "")

    def create_backup(self) -> Path:
        self.backups += 1
        return Path("backup_synthetic.xlsx")

    def apply_marks(
        self,
        sheet: str,
        column: int,
        header: str,
        rows: frozenset[int],
        *,
        expected_revision: str,
    ) -> None:
        assert self.backups > 0
        if expected_revision != self.revision():
            raise ConcurrencyError("Change simulated.")
        self.writes.append((sheet, column, header, rows, expected_revision))
        if column not in {catalog_index for catalog_index, _ in self.headers[sheet]}:
            self.headers[sheet] = (*self.headers[sheet], (column, header))
            self.sheets = tuple(
                replace(
                    info,
                    first_week_column=info.first_week_column or column,
                    last_week_column=column,
                )
                if info.name == sheet
                else info
                for info in self.sheets
            )
        self.marks[(sheet, column)] = rows
        self.number_revision += 1


@dataclass
class Environment:
    source: InMemorySpreadsheet
    catalog: ClientCatalog
    week: WeeklyContacts
    resolution: WeeklyResolution

    def service(self, first: Mapping[str, int] | None = None) -> RegistrationService:
        return RegistrationService(
            reader_clients=self.source,
            writer_clients=self.source,
            source_id="source_synthetic",
            first_columns=first,
        )

    def plan(self, service: RegistrationService | None = None) -> RegistrationPlan:
        return (service or self.service()).plan(
            catalog=self.catalog,
            week=self.week,
            resolution=self.resolution,
            header="Current week",
        )


@pytest.fixture
def environment() -> Environment:
    source = InMemorySpreadsheet()
    catalog = build_catalog(
        source_id="source_synthetic",
        revision=source.revision(),
        generated_at=datetime(2026, 11, 5, tzinfo=UTC),
        sheets=source.sheets,
        clients_by_sheet=source.clients,
    )
    contact = WeeklyContact(raw_name="Axa Sample", last_activity_date=date(2026, 11, 5))
    person = next(person for person in catalog.people if person.sorted_name_key == "axa sample")
    return Environment(
        source=source,
        catalog=catalog,
        week=WeeklyContacts(
            period=WeeklyPeriod(start_date=date(2026, 11, 2), end_date=date(2026, 11, 6)),
            contacts=(contact,),
        ),
        resolution=WeeklyResolution(
            resolved=(
                ResolvedContact(
                    contact=contact,
                    person_id=person.person_id,
                    locations=person.locations,
                    score=100.0,
                    method=MatchMethod.EXACT,
                ),
            )
        ),
    )


def test_constructor_no_query_the_ports(environment: Environment, mocker: MockerFixture) -> None:
    read = mocker.spy(environment.source, "revision")
    backup = mocker.spy(environment.source, "create_backup")
    environment.service()
    read.assert_not_called()
    backup.assert_not_called()


def test_rejects_identity_empty(environment: Environment) -> None:
    with pytest.raises(ConfigurationError):
        RegistrationService(
            reader_clients=environment.source,
            writer_clients=environment.source,
            source_id=" ",
        )


def test_plan_recovers_week_legacy_and_all_the_rows_without_write(environment: Environment) -> None:
    plan = environment.plan()
    assert tuple(
        (operation.sheet, operation.column, operation.rows, operation.new_week)
        for operation in plan.operations
    ) == (
        ("IOL", 3, frozenset({3, 8}), False),
        ("Balanz", 4, frozenset({3}), True),
        ("PPI", 3, frozenset(), True),
    )
    assert plan.marks_previous == (frozenset({5}), frozenset(), frozenset())
    assert environment.source.backups == 0
    assert environment.source.writes == []


def test_dry_run_is_default_and_revalidates_without_effects(environment: Environment) -> None:
    service = environment.service()
    plan = environment.plan(service)
    result = service.run(plan)
    assert result.dry_run
    assert result.plan == plan
    assert result.backup is None
    assert result.applied_sheets == ()
    assert result.revision_final == "revision_0"
    assert environment.source.backups == 0
    assert environment.source.writes == []


def test_applies_after_one_backup_and_refreshes_revision_between_sheets(
    environment: Environment,
) -> None:
    result = environment.service().run(environment.plan(), dry_run=False)
    assert result.applied_sheets == ("IOL", "Balanz", "PPI")
    assert result.backup == Path("backup_synthetic.xlsx")
    assert environment.source.backups == 1
    assert tuple(write[-1] for write in environment.source.writes) == (
        "revision_0",
        "revision_1",
        "revision_2",
    )
    assert result.revision_final == "revision_3"
    assert environment.source.marks[("IOL", 3)] == frozenset({3, 8})
    assert environment.source.marks[("IOL", 5)] == frozenset({3})


def test_week_new_appends_after_of_the_last_without_fill_gaps(environment: Environment) -> None:
    plan = environment.service().plan(
        catalog=environment.catalog,
        week=environment.week,
        resolution=environment.resolution,
        header="Other week",
    )
    assert plan.operations[0].column == 6


def test_repetition_with_catalog_refreshed_no_backs_up_nor_rewrites(
    environment: Environment,
) -> None:
    service = environment.service()
    service.run(environment.plan(), dry_run=False)
    environment.catalog = build_catalog(
        source_id="source_synthetic",
        revision=environment.source.revision(),
        generated_at=environment.catalog.generated_at,
        sheets=environment.source.sheets,
        clients_by_sheet=environment.source.clients,
        previous=environment.catalog,
    )
    result = service.run(environment.plan(), dry_run=False)
    assert not result.dry_run
    assert result.backup is None
    assert result.applied_sheets == ()
    assert environment.source.backups == 1
    assert len(environment.source.writes) == 3


def test_catalog_of_other_source_no_backs_up(environment: Environment) -> None:
    environment.catalog = replace(environment.catalog, source_id="other")
    with pytest.raises(ConfigurationError):
        environment.plan()
    assert environment.source.backups == 0


@pytest.mark.parametrize("dry_run", [True, False])
def test_change_between_plan_and_execution_invalid_the_plan(
    environment: Environment, dry_run: bool
) -> None:
    plan = environment.plan()
    environment.source.number_revision += 1
    with pytest.raises(ConcurrencyError):
        environment.service().run(plan, dry_run=dry_run)
    assert environment.source.backups == 0
    assert environment.source.writes == []


@pytest.mark.parametrize("change", ["clients", "headers", "structure"])
def test_compares_content_and_no_only_the_identifier_of_revision(
    environment: Environment, change: str
) -> None:
    plan = environment.plan()
    if change == "clients":
        environment.source.clients["IOL"] = (ClientRow(row=3, raw_name="Other Person"),)
    elif change == "headers":
        environment.source.headers["IOL"] = ((3, "Current week"), (5, "Changed"))
    else:
        environment.source.sheets = (
            *environment.source.sheets[:-1],
            replace(environment.source.sheets[-1], sheet_id=9),
        )
    with pytest.raises(ConcurrencyError):
        environment.service().run(plan, dry_run=False)
    assert environment.source.backups == 0


def test_change_of_marks_without_revision_invalid_preconditions(environment: Environment) -> None:
    plan = environment.plan()
    environment.source.marks[("IOL", 3)] = frozenset({3})
    with pytest.raises(ConcurrencyError):
        environment.service().run(plan, dry_run=False)
    assert environment.source.backups == 0


def test_plan_tampered_no_applies(environment: Environment) -> None:
    plan = environment.plan()
    tampered = replace(
        plan,
        operations=(replace(plan.operations[0], rows=frozenset({5})), *plan.operations[1:]),
    )
    with pytest.raises(ConcurrencyError):
        environment.service().run(tampered, dry_run=False)
    assert environment.source.backups == 0


def test_failure_backup_propagates_without_write(
    environment: Environment, mocker: MockerFixture
) -> None:
    cause = PersistenceError("Backup failure.")
    mocker.patch.object(environment.source, "create_backup", side_effect=cause)
    with pytest.raises(PersistenceError) as error:
        environment.service().run(environment.plan(), dry_run=False)
    assert error.value is cause
    assert environment.source.writes == []


def test_change_during_backup_preserves_path_without_write(
    environment: Environment, mocker: MockerFixture
) -> None:
    def back_up_with_change() -> Path:
        environment.source.number_revision += 1
        return Path("backup_previous.xlsx")

    mocker.patch.object(environment.source, "create_backup", side_effect=back_up_with_change)
    with pytest.raises(RegistrationInterruptedError) as error:
        environment.service().run(environment.plan(), dry_run=False)
    assert isinstance(error.value.__cause__, ConcurrencyError)
    assert error.value.backup == Path("backup_previous.xlsx")
    assert error.value.confirmed_sheets == ()
    assert error.value.current_sheet is None
    assert environment.source.writes == []


@pytest.mark.parametrize(
    "cause",
    [
        PersistenceError("Failure."),
        OperationTimeoutError("Uncertain."),
        RuntimeError("Untranslated failure."),
    ],
)
def test_failure_intermediate_reports_state_and_no_retries(
    environment: Environment, mocker: MockerFixture, cause: Exception
) -> None:
    original_write = environment.source.apply_marks

    def apply(
        sheet: str, column: int, header: str, rows: frozenset[int], *, expected_revision: str
    ) -> None:
        if sheet == "Balanz":
            raise cause
        original_write(sheet, column, header, rows, expected_revision=expected_revision)

    write = mocker.patch.object(environment.source, "apply_marks", side_effect=apply)
    with pytest.raises(RegistrationInterruptedError) as error:
        environment.service().run(environment.plan(), dry_run=False)
    assert error.value.__cause__ is cause
    assert isinstance(error.value, AutomationError)
    assert error.value.CODE == "REGISTRATION_INTERRUPTED"
    assert error.value.confirmed_sheets == ("IOL",)
    assert error.value.current_sheet == "Balanz"
    assert write.call_count == 2
    assert environment.source.headers["PPI"] == ()


def test_no_confirms_one_write_that_the_source_no_reflects(
    environment: Environment, mocker: MockerFixture
) -> None:
    write = mocker.patch.object(environment.source, "apply_marks", return_value=None)
    with pytest.raises(RegistrationInterruptedError) as error:
        environment.service().run(environment.plan(), dry_run=False)
    assert isinstance(error.value.__cause__, ConcurrencyError)
    assert error.value.confirmed_sheets == ()
    assert error.value.current_sheet == "IOL"
    assert write.call_count == 1


def test_change_of_clients_after_first_write_stops_the_other(
    environment: Environment, mocker: MockerFixture
) -> None:
    original_write = environment.source.apply_marks

    def write_and_move(
        sheet: str, column: int, header: str, rows: frozenset[int], *, expected_revision: str
    ) -> None:
        original_write(sheet, column, header, rows, expected_revision=expected_revision)
        environment.source.clients["Balanz"] = (ClientRow(row=3, raw_name="Client external"),)

    write = mocker.patch.object(environment.source, "apply_marks", side_effect=write_and_move)
    with pytest.raises(RegistrationInterruptedError):
        environment.service().run(environment.plan(), dry_run=False)
    assert write.call_count == 1


def test_revision_changing_during_read_invalid_plan(
    environment: Environment, mocker: MockerFixture
) -> None:
    read = environment.source.read_clients

    def read_and_change(sheet: str) -> tuple[ClientRow, ...]:
        environment.source.number_revision += 1
        return read(sheet)

    mocker.patch.object(environment.source, "read_clients", side_effect=read_and_change)
    with pytest.raises(ConcurrencyError):
        environment.plan()


def test_change_before_of_verification_invalid_revision_original(
    environment: Environment, mocker: MockerFixture
) -> None:
    original_query = environment.source.read_marks

    def query(sheet: str, column: int) -> frozenset[int]:
        environment.source.number_revision = 1
        return original_query(sheet, column)

    mocker.patch.object(environment.source, "read_marks", side_effect=query)
    with pytest.raises(ConcurrencyError):
        environment.plan()


def test_table_custom_copy_and_no_imports_constants_of_excel(environment: Environment) -> None:
    first = {"IOL": 3, "Balanz": 6, "PPI": 7}
    service = environment.service(first)
    first["Balanz"] = 9
    assert environment.plan(service).operations[1].column == 6


@pytest.mark.parametrize("first", [{}, {"IOL": 1}, {"IOL": True}])
def test_configuration_of_columns_incomplete_or_invalid_rejects(
    environment: Environment, first: dict[str, int]
) -> None:
    with pytest.raises(DomainValidationError):
        environment.plan(environment.service(first))


@pytest.mark.parametrize("header", ["", " Current week", "Current week "])
def test_header_must_be_canonical(environment: Environment, header: str) -> None:
    with pytest.raises(DomainValidationError):
        environment.service().plan(
            catalog=environment.catalog,
            week=environment.week,
            resolution=environment.resolution,
            header=header,
        )


def test_pending_block_by_default_and_its_usage_partial_is_explicit(
    environment: Environment,
) -> None:
    contact = WeeklyContact(raw_name="No match", last_activity_date=date(2026, 11, 5))
    environment.week = replace(environment.week, contacts=(*environment.week.contacts, contact))
    environment.resolution = replace(
        environment.resolution,
        pending=(PendingContact(contact=contact, reason=PendingReason.NO_MATCH),),
    )
    with pytest.raises(DomainValidationError, match="pending"):
        environment.plan()
    plan = environment.service().plan(
        catalog=environment.catalog,
        week=environment.week,
        resolution=environment.resolution,
        header="Current week",
        allow_pending=True,
    )
    assert plan.operations[0].rows == frozenset({3, 8})
    assert plan.allow_pending


@pytest.mark.parametrize("case", ["missing", "foreign", "duplicate_omitted"])
def test_resolution_must_cover_the_week_received(environment: Environment, case: str) -> None:
    if case == "missing":
        environment.resolution = WeeklyResolution()
    elif case == "foreign":
        environment.week = replace(environment.week, contacts=())
    else:
        environment.week = replace(environment.week, contacts=environment.week.contacts * 2)
    with pytest.raises(DomainValidationError, match="cover"):
        environment.plan()


def test_contacts_repeated_and_holders_no_duplicate_the_rows(environment: Environment) -> None:
    environment.week = replace(environment.week, contacts=environment.week.contacts * 2)
    environment.resolution = replace(
        environment.resolution, resolved=environment.resolution.resolved * 2
    )
    assert environment.plan().operations[0].rows == frozenset({3, 8})


@pytest.mark.parametrize("case", ["person_missing", "row_unrelated", "row_omitted", "group"])
def test_rejects_destinations_unrelated_partial_and_groups(
    environment: Environment, case: str
) -> None:
    resolved = environment.resolution.resolved[0]
    if case == "person_missing":
        resolved = replace(resolved, person_id="nonexistent")
    elif case == "row_unrelated":
        resolved = replace(resolved, locations=(ClientLocation(sheet="IOL", row=5),))
    elif case == "row_omitted":
        resolved = replace(resolved, locations=(ClientLocation(sheet="IOL", row=3),))
    else:
        contact = replace(resolved.contact, is_group=True)
        resolved = replace(resolved, contact=contact)
        environment.week = replace(environment.week, contacts=(contact,))
    environment.resolution = replace(environment.resolution, resolved=(resolved,))
    with pytest.raises(DomainValidationError):
        environment.plan()


def test_respect_brokers_allows_subset_of_sheets_with_all_their_rows(
    environment: Environment,
) -> None:
    resolved = environment.resolution.resolved[0]
    environment.resolution = replace(
        environment.resolution,
        resolved=(
            replace(
                resolved,
                locations=tuple(
                    location for location in resolved.locations if location.sheet == "IOL"
                ),
            ),
        ),
    )
    plan = environment.plan()
    assert plan.operations[0].rows == frozenset({3, 8})
    assert plan.operations[1].rows == frozenset()


def test_week_empty_is_one_replacement_explicit_without_marks(environment: Environment) -> None:
    environment.week = replace(environment.week, contacts=())
    environment.resolution = WeeklyResolution()
    assert all(not operation.rows for operation in environment.plan().operations)


def test_groups_ignored_cover_contacts_without_mark(environment: Environment) -> None:
    group = replace(environment.week.contacts[0], is_group=True)
    environment.week = replace(environment.week, contacts=(group,))
    environment.resolution = WeeklyResolution(
        ignored=(IgnoredContact(contact=group, reason=IgnoredReason.GROUP),)
    )
    assert all(not operation.rows for operation in environment.plan().operations)


@pytest.mark.parametrize(
    "columns",
    [
        ((3, "Current week"), (3, "Duplicate")),
        ((5, "Later"), (3, "Current week")),
        ((2, "Fixed"), (5, "Later")),
        ((3, ""), (5, "Later")),
        ((3, "Current week"),),
        ((3, "Current week"), (5, " Current week ")),
    ],
)
def test_headers_ambiguous_or_outside_of_structure_no_produce_plan(
    environment: Environment, columns: Headers
) -> None:
    environment.source.headers["IOL"] = columns
    with pytest.raises(DomainValidationError):
        environment.plan()


def test_missing_of_sheets_or_headers_no_produces_operations(environment: Environment) -> None:
    with pytest.raises(DomainValidationError):
        build_operations(
            catalog=environment.catalog,
            week=environment.week,
            resolution=environment.resolution,
            header="Current week",
            headers={},
            first_columns={},
        )
    environment.catalog = replace(environment.catalog, sheets=(), people=())
    environment.week = replace(environment.week, contacts=())
    environment.resolution = WeeklyResolution()
    with pytest.raises(DomainValidationError):
        environment.plan()


def test_catalog_rejects_texts_conflicting_in_one_row_shared(environment: Environment) -> None:
    first, second = environment.catalog.people
    location = first.occurrences[0].location
    second = replace(
        second,
        occurrences=(ClientOccurrence(location=location, raw_name="Conflicting text"),),
    )
    with pytest.raises(DomainValidationError):
        catalog_clients(replace(environment.catalog, people=(first, second)))


def test_models_immutable_and_representation_without_data_personal(
    environment: Environment,
) -> None:
    plan = environment.plan()
    with pytest.raises(FrozenInstanceError):
        plan.__setattr__("header", "Change")
    assert "Axa Sample" not in repr(plan)
    assert "Notuka Example" not in repr(plan)


@pytest.mark.parametrize(
    ("column", "rows"),
    [(0, frozenset({3})), (True, frozenset({3})), (3, frozenset({True})), (3, frozenset({0}))],
)
def test_operations_reject_indices_invalid(column: int, rows: frozenset[int]) -> None:
    with pytest.raises(DomainValidationError):
        SheetMarks(sheet="IOL", column=column, header="Week", rows=rows, new_week=False)


def test_operation_requires_texts_no_empty() -> None:
    with pytest.raises(DomainValidationError):
        SheetMarks(sheet=" ", column=3, header="Week", rows=frozenset(), new_week=False)


@pytest.mark.parametrize("field", ["operations", "headers_previous", "marks_previous"])
def test_plan_requires_preconditions_aligned(environment: Environment, field: str) -> None:
    plan = environment.plan()
    with pytest.raises(DomainValidationError):
        if field == "operations":
            replace(plan, operations=())
        elif field == "headers_previous":
            replace(plan, headers_previous=())
        else:
            replace(plan, marks_previous=())


@pytest.mark.parametrize(
    "case", ["dry_run", "without_backup", "revision", "sheet_unrelated", "duplicate"]
)
def test_result_rejects_effects_inconsistent(environment: Environment, case: str) -> None:
    result = RegistrationResult(
        plan=environment.plan(),
        dry_run=False,
        backup=Path("copy.xlsx"),
        applied_sheets=("IOL",),
        revision_final="revision_final",
    )
    with pytest.raises(DomainValidationError):
        if case == "dry_run":
            replace(result, dry_run=True)
        elif case == "without_backup":
            replace(result, backup=None)
        elif case == "revision":
            replace(result, revision_final=" ")
        elif case == "sheet_unrelated":
            replace(result, applied_sheets=("Unrelated",))
        else:
            replace(result, applied_sheets=("IOL", "IOL"))


def test_logs_no_expose_cause_paths_or_contacts(
    environment: Environment, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
) -> None:
    mocker.patch.object(
        environment.source, "apply_marks", side_effect=PersistenceError("Axa Sample; path secret")
    )
    with caplog.at_level("INFO"), pytest.raises(RegistrationInterruptedError) as error:
        environment.service().run(environment.plan(), dry_run=False)
    assert "Registration interrupted" in caplog.text
    assert "Axa Sample" not in caplog.text
    assert "path secret" not in caplog.text
    assert "backup_synthetic" not in caplog.text
    assert "path secret" not in str(error.value)
