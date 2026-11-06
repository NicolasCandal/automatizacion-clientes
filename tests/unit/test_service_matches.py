"""Orchestration with mock ports, without network, files, or credentials."""

import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from client_automation.core.aliases import propose_alias, reconcile_alias
from client_automation.core.contracts import (
    AliasStorePort,
    CatalogStore,
    ContactReader,
    ExclusionListPort,
    NameComparator,
    ResolutionStore,
)
from client_automation.core.errors import (
    ConcurrencyError,
    ConfigurationError,
    DomainValidationError,
    PersistenceError,
)
from client_automation.core.models import (
    AliasDestination,
    ClientCatalog,
    ClientLocation,
    ClientOccurrence,
    ClientPerson,
    ContactAlias,
    IgnoredReason,
    MatchingPolicy,
    MatchMethod,
    PendingReason,
    SheetInfo,
    WeeklyContact,
    WeeklyContacts,
    WeeklyPeriod,
    WeeklyResolution,
)
from client_automation.core.normalization import sorted_name_key
from client_automation.services.matching import MatchingService


@dataclass
class Environment:
    reader: MagicMock
    catalogs: MagicMock
    aliases: MagicMock
    exclusions: MagicMock
    reports: MagicMock
    comparator: MagicMock

    def service(
        self,
        policy: MatchingPolicy | None = None,
        table_brokers: Mapping[str, str] | None = None,
    ) -> MatchingService:
        return MatchingService(
            reader_contacts=self.reader,
            store_catalog=self.catalogs,
            store_aliases=self.aliases,
            list_exclusion=self.exclusions,
            store_resolutions=self.reports,
            comparator=self.comparator,
            policy=policy,
            table_brokers=table_brokers,
        )


@pytest.fixture
def contact() -> WeeklyContact:
    return WeeklyContact(
        raw_name="Axa Sample (IOL)", last_activity_date=date(2026, 11, 5), phone="+5491100000000"
    )


@pytest.fixture
def week(contact: WeeklyContact) -> WeeklyContacts:
    return WeeklyContacts(
        period=WeeklyPeriod(start_date=date(2026, 11, 2), end_date=date(2026, 11, 6)),
        contacts=(contact,),
    )


@pytest.fixture
def catalog() -> ClientCatalog:
    return ClientCatalog(
        source_id="source_synthetic",
        revision="revision_1",
        generated_at=datetime(2026, 11, 5, tzinfo=UTC),
        sheets=(
            SheetInfo(
                name="IOL",
                sheet_id=0,
                name_column=1,
                first_week_column=None,
                last_week_column=None,
                last_client_row=100,
            ),
        ),
        people=tuple(
            ClientPerson(
                person_id=identifier,
                sorted_name_key=sorted_name_key(name),
                canonical_name=name,
                holders=(name,),
                occurrences=(
                    ClientOccurrence(location=ClientLocation(sheet="IOL", row=row), raw_name=name),
                ),
            )
            for identifier, name, row in (
                ("p0001", "Axa Sample", 3),
                ("p0002", "Notuka Example", 4),
            )
        ),
    )


@pytest.fixture
def environment(mocker: MockerFixture, week: WeeklyContacts, catalog: ClientCatalog) -> Environment:
    sources = Environment(
        reader=mocker.create_autospec(ContactReader, instance=True, spec_set=True),
        catalogs=mocker.create_autospec(CatalogStore, instance=True, spec_set=True),
        aliases=mocker.create_autospec(AliasStorePort, instance=True, spec_set=True),
        exclusions=mocker.create_autospec(ExclusionListPort, instance=True, spec_set=True),
        reports=mocker.create_autospec(ResolutionStore, instance=True, spec_set=True),
        comparator=mocker.create_autospec(NameComparator, instance=True, spec_set=True),
    )
    sources.reader.read.return_value = week
    sources.catalogs.read.return_value = catalog
    sources.aliases.find_by_name.return_value = None
    sources.aliases.find_by_phone.return_value = ()
    sources.aliases.register.return_value = None
    sources.exclusions.contains.return_value = False
    sources.comparator.similarity.return_value = 0.0
    return sources


def test_constructor_no_runs_ports(environment: Environment) -> None:
    environment.service()
    assert not environment.reader.mock_calls
    assert not environment.catalogs.mock_calls
    assert not environment.aliases.mock_calls
    assert not environment.exclusions.mock_calls
    assert not environment.reports.mock_calls


def test_snapshots_of_flow_avoid_reread_catalog_and_contacts(
    environment: Environment,
    week: WeeklyContacts,
    catalog: ClientCatalog,
) -> None:
    environment.reader.read.side_effect = RuntimeError("The source must not be read again.")
    environment.catalogs.read.side_effect = RuntimeError("The source must not be read again.")
    result = environment.service().run(catalog=catalog, week=week)
    assert len(result.resolved) == 1
    environment.reader.read.assert_not_called()
    environment.catalogs.read.assert_not_called()


def test_dry_run_default_no_registers_nor_persists(environment: Environment) -> None:
    report = environment.service().run()
    assert len(report.resolved) == 1
    assert report.resolved[0].method is MatchMethod.EXACT
    environment.aliases.register.assert_not_called()
    environment.aliases.save.assert_not_called()
    environment.reports.save.assert_not_called()
    environment.exclusions.add.assert_not_called()
    environment.catalogs.save.assert_not_called()


def test_real_saves_report_before_of_learn_and_saves_aliases_one_time(
    environment: Environment, contact: WeeklyContact, mocker: MockerFixture
) -> None:
    sequence = mocker.Mock()
    sequence.attach_mock(environment.reports.save, "report")
    sequence.attach_mock(environment.aliases.register, "register")
    sequence.attach_mock(environment.aliases.save, "save")

    report = environment.service().run(dry_run=False)

    assert [call[0] for call in sequence.mock_calls] == ["report", "register", "save"]
    environment.reports.save.assert_called_once_with(report)
    environment.aliases.register.assert_called_once_with(
        ContactAlias(
            raw_name=contact.raw_name,
            phone=contact.phone,
            people=(AliasDestination(person_id="p0001", sheets=frozenset({"IOL"})),),
        )
    )


def test_absence_of_catalog_no_starts_learning(environment: Environment) -> None:
    environment.catalogs.read.return_value = None
    with pytest.raises(ConfigurationError, match="catalog"):
        environment.service().run(dry_run=False)
    assert not environment.aliases.mock_calls
    environment.reports.save.assert_not_called()


@pytest.mark.parametrize("group", [True, False])
def test_groups_and_excluded_no_query_aliases(
    environment: Environment, week: WeeklyContacts, contact: WeeklyContact, group: bool
) -> None:
    environment.reader.read.return_value = replace(
        week, contacts=(replace(contact, is_group=group),)
    )
    environment.exclusions.contains.return_value = True
    report = environment.service().run(dry_run=False)
    assert report.ignored[0].reason is (IgnoredReason.GROUP if group else IgnoredReason.EXCLUDED)
    assert not environment.aliases.mock_calls
    if group:
        environment.exclusions.contains.assert_not_called()


def test_phone_shared_returns_complete_without_choose_first(
    environment: Environment, contact: WeeklyContact
) -> None:
    environment.aliases.find_by_phone.return_value = tuple(
        ContactAlias(
            raw_name=name,
            phone=contact.phone,
            people=(AliasDestination(person_id=identifier, sheets=frozenset({"IOL"})),),
        )
        for name, identifier in (("Alias A", "p0001"), ("Alias B", "p0002"))
    )
    report = environment.service().run(dry_run=False)
    assert report.pending[0].reason is PendingReason.AMBIGUOUS
    environment.aliases.find_by_name.assert_not_called()
    environment.aliases.register.assert_not_called()
    environment.aliases.save.assert_not_called()


@pytest.mark.parametrize("method", [MatchMethod.ALIAS, MatchMethod.PHONE])
def test_decisions_by_alias_no_relearn(
    environment: Environment, contact: WeeklyContact, method: MatchMethod
) -> None:
    alias = ContactAlias(
        raw_name=contact.raw_name,
        phone=contact.phone,
        people=(AliasDestination(person_id="p0001", sheets=frozenset({"IOL"})),),
    )
    if method is MatchMethod.ALIAS:
        environment.aliases.find_by_name.return_value = alias
    else:
        environment.aliases.find_by_phone.return_value = (alias,)
    report = environment.service().run(dry_run=False)
    assert report.resolved[0].method is method
    environment.aliases.register.assert_not_called()
    environment.aliases.save.assert_not_called()


def test_no_learns_of_contacts_partially_resolved(
    environment: Environment, week: WeeklyContacts, contact: WeeklyContact
) -> None:
    environment.reader.read.return_value = replace(
        week, contacts=(replace(contact, raw_name="Axa Sample AND/OR ZZZZZZZ (IOL)"),)
    )
    report = environment.service().run(dry_run=False)
    assert len(report.resolved) == len(report.pending) == 1
    environment.aliases.register.assert_not_called()
    environment.aliases.save.assert_not_called()


def test_holders_learn_together_and_no_cache_rows(
    environment: Environment, week: WeeklyContacts, contact: WeeklyContact
) -> None:
    environment.reader.read.return_value = replace(
        week, contacts=(replace(contact, raw_name="Axa Sample AND/OR Notuka Example (IOL)"),)
    )
    report = environment.service().run(dry_run=False)
    assert len(report.resolved) == 2
    environment.aliases.register.assert_called_once()
    alias: ContactAlias = environment.aliases.register.call_args.args[0]
    assert tuple(destination.person_id for destination in alias.people) == ("p0001", "p0002")
    assert not hasattr(alias.people[0], "row")


def test_learning_no_changes_decisions_of_other_contact_of_same_phone(
    environment: Environment, week: WeeklyContacts, contact: WeeklyContact
) -> None:
    environment.reader.read.return_value = replace(
        week, contacts=(contact, replace(contact, raw_name="Notuka Example (IOL)"))
    )
    report = environment.service().run(dry_run=False)
    assert tuple(result.person_id for result in report.resolved) == ("p0001", "p0002")
    assert all(result.method is MatchMethod.EXACT for result in report.resolved)
    environment.aliases.find_by_phone.assert_called_once_with(contact.phone)


def test_dry_run_and_real_return_same_warnings_of_phone_in_conflict(
    environment: Environment, week: WeeklyContacts, contact: WeeklyContact
) -> None:
    environment.reader.read.return_value = replace(
        week, contacts=(contact, replace(contact, phone="+5491199999999"))
    )
    service = environment.service()
    simulated = service.run()
    environment.aliases.register.assert_not_called()
    real = service.run(dry_run=False)
    assert simulated == real
    assert len(real.warnings) == 1
    assert "different phone number" in real.warnings[0]
    assert contact.raw_name not in real.warnings[0]
    environment.aliases.register.assert_called_once()
    alias: ContactAlias = environment.aliases.register.call_args.args[0]
    assert alias.phone == contact.phone


def test_repetitions_equivalent_complete_phone_without_duplicate_registration(
    environment: Environment, week: WeeklyContacts, contact: WeeklyContact
) -> None:
    environment.reader.read.return_value = replace(
        week, contacts=(replace(contact, phone=None), contact, contact)
    )
    report = environment.service().run(dry_run=False)
    assert len(report.resolved) == 3
    assert not report.warnings
    environment.aliases.register.assert_called_once()
    alias: ContactAlias = environment.aliases.register.call_args.args[0]
    assert alias.phone == contact.phone
    environment.aliases.find_by_name.assert_called_once_with(contact.raw_name)


def test_policy_configured_controls_comparison_fuzzy(
    environment: Environment, catalog: ClientCatalog, week: WeeklyContacts, contact: WeeklyContact
) -> None:
    environment.catalogs.read.return_value = replace(catalog, people=catalog.people[:1])
    environment.reader.read.return_value = replace(
        week, contacts=(replace(contact, raw_name="Query different"),)
    )
    environment.comparator.similarity.return_value = 90.0
    assert environment.service().run().pending
    policy = MatchingPolicy(automatic_threshold=89.0, review_threshold=80.0)
    assert environment.service(policy).run().resolved[0].method is MatchMethod.FUZZY


def test_week_empty_saves_report_without_touch_aliases(
    environment: Environment, week: WeeklyContacts
) -> None:
    environment.reader.read.return_value = replace(week, contacts=())
    report = environment.service().run(dry_run=False)
    assert report == WeeklyResolution()
    environment.reports.save.assert_called_once_with(report)
    assert not environment.aliases.mock_calls


def test_table_of_brokers_respects_and_copy_on_build_service(
    environment: Environment, week: WeeklyContacts, contact: WeeklyContact
) -> None:
    environment.reader.read.return_value = replace(
        week, contacts=(replace(contact, raw_name="Axa Sample (selector)"),)
    )
    table = {"selector": "Bull"}
    service = environment.service(table_brokers=table)
    table["selector"] = "IOL"

    report = service.run()

    assert report.pending[0].reason is PendingReason.BROKER_MISMATCH


@pytest.mark.parametrize(
    "stage", ["contacts", "catalog", "exclusions", "phone", "name", "comparator"]
)
def test_failure_during_resolution_no_persists_nor_starts_learning(
    environment: Environment, week: WeeklyContacts, contact: WeeklyContact, stage: str
) -> None:
    cause = PersistenceError("Test error without personal information")
    operations = {
        "contacts": environment.reader.read,
        "catalog": environment.catalogs.read,
        "exclusions": environment.exclusions.contains,
        "phone": environment.aliases.find_by_phone,
        "name": environment.aliases.find_by_name,
        "comparator": environment.comparator.similarity,
    }
    if stage == "comparator":
        environment.reader.read.return_value = replace(
            week, contacts=(contact, replace(contact, raw_name="ZZZZZZZ"))
        )
    operations[stage].side_effect = cause
    with pytest.raises(PersistenceError) as captured:
        environment.service().run(dry_run=False)
    assert captured.value is cause
    environment.aliases.register.assert_not_called()
    environment.aliases.save.assert_not_called()
    environment.reports.save.assert_not_called()


def test_failure_of_report_no_modifies_aliases_and_allows_retry(environment: Environment) -> None:
    environment.reports.save.side_effect = PersistenceError("locked")
    service = environment.service()
    with pytest.raises(PersistenceError):
        service.run(dry_run=False)
    environment.aliases.register.assert_not_called()
    environment.aliases.save.assert_not_called()
    environment.reports.save.side_effect = None
    assert service.run(dry_run=False).resolved


@pytest.mark.parametrize("stage", ["register", "save", "conflict"])
def test_failure_of_aliases_leaves_report_and_requires_new_session(
    environment: Environment, stage: str
) -> None:
    if stage == "conflict":
        environment.aliases.register.return_value = "Conflict detectado"
    else:
        getattr(environment.aliases, stage).side_effect = PersistenceError("locked")
    service = environment.service()
    with pytest.raises(ConcurrencyError if stage == "conflict" else PersistenceError):
        service.run(dry_run=False)
    environment.reports.save.assert_called_once()
    with pytest.raises(ConfigurationError, match="new session"):
        service.run()
    environment.reader.read.assert_called_once()


def test_rules_pure_reject_names_and_results_of_other_contact(
    environment: Environment, contact: WeeklyContact
) -> None:
    result = environment.service().run().resolved[0]
    alias = ContactAlias(
        raw_name=contact.raw_name,
        people=(AliasDestination(person_id="p0001", sheets=frozenset({"IOL"})),),
    )
    with pytest.raises(DomainValidationError):
        reconcile_alias(alias, replace(alias, raw_name="Other name"))
    with pytest.raises(DomainValidationError):
        propose_alias(replace(contact, raw_name="Other name"), (result,))
    assert propose_alias(contact, ()) is None


@pytest.mark.parametrize("failure_aliases", [False, True])
def test_logs_no_include_identities_nor_details_technical_private(
    environment: Environment, caplog: pytest.LogCaptureFixture, failure_aliases: bool
) -> None:
    if failure_aliases:
        environment.aliases.save.side_effect = PersistenceError("Detail secret of +5491100000000")
    with caplog.at_level(logging.INFO):
        if failure_aliases:
            with pytest.raises(PersistenceError):
                environment.service().run(dry_run=False)
        else:
            environment.service().run(dry_run=False)

    assert len(caplog.records) == 1
    assert caplog.records[0].exc_info is None
    for identity in ("Axa Sample", "+5491100000000", "p0001", "secret"):
        assert identity not in caplog.text
    assert ("Matching completed" in caplog.text) is not failure_aliases
