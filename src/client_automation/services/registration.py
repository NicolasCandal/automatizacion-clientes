"""Weekly planning, dry runs, and registration through independent ports."""

import logging
from collections.abc import Mapping
from dataclasses import replace

from client_automation.core.contracts import ClientWriter, RegistrationClientReader
from client_automation.core.errors import (
    ConcurrencyError,
    ConfigurationError,
    RegistrationInterruptedError,
)
from client_automation.core.models import (
    ClientCatalog,
    WeeklyContacts,
    WeeklyResolution,
)
from client_automation.core.registration import (
    Headers,
    RegistrationPlan,
    RegistrationResult,
    build_operations,
    catalog_clients,
)

LOGGER = logging.getLogger(__name__)


class RegistrationService:
    """Plan through reads; apply with backups and verification between sheets."""

    def __init__(
        self,
        *,
        reader_clients: RegistrationClientReader,
        writer_clients: ClientWriter,
        source_id: str,
        first_columns: Mapping[str, int] | None = None,
    ) -> None:
        """Accept dependencies without reads, backups, or writes."""
        if not source_id.strip():
            raise ConfigurationError("A stable source identifier is required.")
        self._reader = reader_clients
        self._writer = writer_clients
        self._source = source_id
        self._first = (
            dict(first_columns)
            if first_columns is not None
            else {
                "Inviu": 3,
                "IOL": 3,
                "Bull": 3,
                "Balanz": 4,
                "PPI": 3,
            }
        )

    def plan(
        self,
        *,
        catalog: ClientCatalog,
        week: WeeklyContacts,
        resolution: WeeklyResolution,
        header: str,
        allow_pending: bool = False,
    ) -> RegistrationPlan:
        """Validate evidence and query the source without performing writes."""
        if catalog.source_id != self._source:
            raise ConfigurationError("The catalog belongs to another source.")
        self._check_revision(catalog.revision)
        headers = {sheet.name: self._reader.read_headers(sheet.name) for sheet in catalog.sheets}
        operations = build_operations(
            catalog=catalog,
            week=week,
            resolution=resolution,
            header=header,
            headers=headers,
            first_columns=self._first,
            allow_pending=allow_pending,
        )
        marks = tuple(
            frozenset()
            if operation.new_week
            else self._reader.read_marks(operation.sheet, operation.column)
            for operation in operations
        )
        plan = RegistrationPlan(
            catalog=catalog,
            week=week,
            resolution=resolution,
            header=header,
            allow_pending=allow_pending,
            operations=operations,
            headers_previous=tuple(headers[operation.sheet] for operation in operations),
            marks_previous=marks,
        )
        revision = self._verify_state(plan, ())
        if revision != catalog.revision:
            raise ConcurrencyError("The source changed during registration planning.")
        LOGGER.info("Registration planned: %s sheets.", len(operations))
        return plan

    def run(self, plan: RegistrationPlan, *, dry_run: bool = True) -> RegistrationResult:
        """Revalidate the plan, default to dry run, and write only changed sheets."""
        current = self.plan(
            catalog=plan.catalog,
            week=plan.week,
            resolution=plan.resolution,
            header=plan.header,
            allow_pending=plan.allow_pending,
        )
        if current != plan:
            raise ConcurrencyError("The plan or its preconditions changed. Plan again.")
        changes = tuple(
            operation
            for operation, previous in zip(plan.operations, plan.marks_previous, strict=True)
            if operation.new_week or operation.rows != previous
        )
        if dry_run or not changes:
            LOGGER.info(
                "Registration without writes: dry_run=%s; %s changed sheets.",
                dry_run,
                len(changes),
            )
            return RegistrationResult(
                plan=plan,
                dry_run=dry_run,
                backup=None,
                applied_sheets=(),
                revision_final=plan.catalog.revision,
            )
        backup = self._writer.create_backup()
        confirmed: list[str] = []
        in_progress: str | None = None
        try:
            revision = self._verify_state(plan, ())
            if revision != plan.catalog.revision:
                raise ConcurrencyError("The source changed during backup creation.")
            for operation in changes:
                in_progress = operation.sheet
                self._writer.apply_marks(
                    operation.sheet,
                    operation.column,
                    operation.header,
                    operation.rows,
                    expected_revision=revision,
                )
                revision = self._verify_state(plan, (*confirmed, operation.sheet))
                confirmed.append(operation.sheet)
                in_progress = None
        except Exception as cause:
            LOGGER.error(
                "Registration interrupted: %s sheets confirmed; result in progress uncertain=%s.",
                len(confirmed),
                in_progress is not None,
            )
            raise RegistrationInterruptedError(
                backup=backup,
                confirmed_sheets=tuple(confirmed),
                current_sheet=in_progress,
            ) from cause
        LOGGER.info("Registration applied and verified: %s sheets.", len(confirmed))
        return RegistrationResult(
            plan=plan,
            dry_run=False,
            backup=backup,
            applied_sheets=tuple(confirmed),
            revision_final=revision,
        )

    def _check_revision(self, expected: str) -> None:
        if self._reader.revision() != expected:
            raise ConcurrencyError("The source changed. Refresh the catalog and resolve again.")

    def _verify_state(self, plan: RegistrationPlan, applied: tuple[str, ...]) -> str:
        revision = self._reader.revision()
        expected = catalog_clients(plan.catalog)
        sheets_expected = []
        for sheet, operation, headers, marks in zip(
            plan.catalog.sheets,
            plan.operations,
            plan.headers_previous,
            plan.marks_previous,
            strict=True,
        ):
            was_applied = operation.sheet in applied
            current: Headers = headers
            if was_applied and operation.new_week:
                current = (*headers, (operation.column, operation.header))
                sheet = replace(
                    sheet,
                    first_week_column=sheet.first_week_column or operation.column,
                    last_week_column=operation.column,
                )
            sheets_expected.append(sheet)
            if (
                self._reader.read_headers(sheet.name) != current
                or self._reader.read_clients(sheet.name) != expected[sheet.name]
            ):
                raise ConcurrencyError("Clients or headers do not match the plan.")
            if not operation.new_week or was_applied:
                current_marks = self._reader.read_marks(sheet.name, operation.column)
                if current_marks != (operation.rows if was_applied else marks):
                    raise ConcurrencyError("Marks do not match the plan's expected state.")
        if self._reader.list_sheets() != tuple(sheets_expected):
            raise ConcurrencyError("The source structure does not match the plan.")
        self._check_revision(revision)
        return revision
