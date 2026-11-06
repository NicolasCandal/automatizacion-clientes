"""Compatibility facade guarantees without live Google access."""

import importlib
import subprocess
import sys
from collections.abc import Iterator
from os import environ
from pathlib import Path

import pytest
from openpyxl import Workbook
from openpyxl.worksheet.worksheet import Worksheet
from pytest_mock import MockerFixture

from client_automation.config import Config
from client_automation.core.errors import (
    AuthenticationError,
    ConcurrencyError,
    ConfigurationError,
    DomainValidationError,
    ExternalServiceError,
    StructureError,
)
from client_automation.infra.legacy_formats import (
    alias_from_legacy,
    catalog_for_matching,
    contact_from_legacy,
)
from matching.alias_store import AliasStore
from matching.engine import build_index, resolve_contact, resolve_week
from matching.exclusion_list import ExclusionList
from repository import auth
from repository.repo_sheets import RepoSheets
from repository.repo_xlsx import RepoXlsx


@pytest.fixture(autouse=True)
def isolate_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(environ):
        if name.upper().startswith("CA_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def workbook(tmp_path: Path) -> Iterator[RepoXlsx]:
    path = tmp_path / "sample.xlsx"
    document = Workbook()
    initial = document.active
    assert isinstance(initial, Worksheet)
    document.remove(initial)
    for name in ("Inviu", "IOL", "Bull", "Balanz", "PPI"):
        sheet = document.create_sheet(name)
        sheet.cell(1, 1, "Client")
        sheet.cell(2, 4 if name == "Balanz" else 3, "Week")
        sheet.cell(3, 1, "Axa Sample")
    document.save(path)
    document.close()
    with RepoXlsx(path) as repository:
        yield repository


@pytest.mark.parametrize("package", ["catalog_index", "matching", "repository"])
@pytest.mark.parametrize("help", [False, True])
def test_commands_retired_no_open_inputs_nor_expose_arguments(
    tmp_path: Path,
    package: str,
    help: bool,
) -> None:
    (tmp_path / ".env").write_text("CA_LOG_LEVEL=value_invalid\n", encoding="utf-8")
    options = ["--help"] if help else ["resolve", "--out", "path_secret.json"]
    process = subprocess.run(
        [sys.executable, "-m", package, *options],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert process.returncode == (0 if help else 2)
    assert process.stdout == ""
    assert "client-automation --help" in process.stderr
    assert "path_secret" not in process.stderr
    assert not (tmp_path / "path_secret.json").exists()


def test_import_contacts_configuration_previous_no_load_dotenv(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "CA_LOG_LEVEL=DEBUG\nVARIABLE_NO_LOADED=secret\n", encoding="utf-8"
    )
    module = importlib.import_module("repository.config")
    importlib.reload(module)
    assert "VARIABLE_NO_LOADED" not in environ
    assert module.Config is Config
    assert module.load_config(path_env=None).log_level == "INFO"


def test_authentication_previous_no_starts_consent(
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    authorize = mocker.patch("webbrowser.open", side_effect=AssertionError("Browser unexpected"))
    with pytest.raises(AuthenticationError):
        auth.get_credentials(tmp_path / "client.json", tmp_path / "token.json")
    authorize.assert_not_called()
    assert list(tmp_path.iterdir()) == []


def test_authentication_previous_delegates_without_save_token(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    provider = mocker.patch.object(auth, "GoogleCredentialsProvider")
    auth.get_credentials(tmp_path / "client.json", tmp_path / "token.json")
    provider.return_value.load.assert_called_once_with(persist=False)
    provider.return_value.authorize.assert_not_called()
    configuration = provider.call_args.args[0]
    assert configuration.credentials_path == tmp_path / "client.json"
    assert configuration.google_scopes == ("https://www.googleapis.com/auth/spreadsheets.readonly",)


def test_write_previous_without_revision_no_modifies_workbook(workbook: RepoXlsx) -> None:
    original = workbook.path.read_bytes()
    with pytest.raises(ConfigurationError):
        workbook.apply_marks("IOL", 3, "Week", {3})
    assert workbook.path.read_bytes() == original


def test_revision_old_rejects_although_use_the_facade(workbook: RepoXlsx) -> None:
    revision = workbook.revision()
    workbook.apply_marks("IOL", 3, "Week", {3}, expected_revision=revision)
    with pytest.raises(ConcurrencyError):
        workbook.apply_marks("IOL", 3, "Week", set(), expected_revision=revision)
    assert workbook.read_marks("IOL", 3) == {3}


def test_closure_of_facade_invalid_read_and_write(workbook: RepoXlsx) -> None:
    workbook.close()
    workbook.close()
    with pytest.raises(ConfigurationError):
        workbook.get_writer()
    with pytest.raises(ConfigurationError):
        workbook.revision()
    with pytest.raises(ConfigurationError), workbook:
        pass


def test_facade_sheets_no_creates_writer_for_read_and_closes_both(
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    module = importlib.import_module("repository.repo_sheets")
    reader = mocker.Mock()
    writer = mocker.Mock()
    mocker.patch.object(module, "create_reader_sheets", return_value=reader)
    create = mocker.patch.object(module, "create_writer_sheets", return_value=writer)
    configuration = Config(base_directory=tmp_path, spreadsheet_id="document_fake", _env_file=None)
    with RepoSheets("document_fake", mocker.Mock(), configuration=configuration) as repo:
        create.assert_not_called()
        assert repo.get_writer() is writer
        assert repo.get_writer() is writer
        create.assert_called_once()
    reader.close.assert_called_once()
    create.return_value.close.assert_called_once()
    repo.close()
    with pytest.raises(ConfigurationError):
        repo.get_writer()


def test_facade_sheets_rejects_document_incompatible_before_of_create_session(
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    module = importlib.import_module("repository.repo_sheets")
    create = mocker.patch.object(module, "create_reader_sheets")
    configuration = Config(base_directory=tmp_path, spreadsheet_id="other", _env_file=None)
    with pytest.raises(ConfigurationError):
        RepoSheets("document_fake", mocker.Mock(), configuration=configuration)
    create.assert_not_called()


def test_aliases_legacy_delegate_reconciliation_and_no_share_dictionaries(
    tmp_path: Path,
) -> None:
    store = AliasStore(tmp_path / "aliases.json")
    assert len(store) == 0
    assert store.register("Axa", [{"person_id": "person_1", "sheets": ["IOL"]}], "+123") is None
    input = store.find_by_name("Axa")
    assert input is not None
    input["people"].clear()
    assert store.find_by_name("Axa") != input
    assert len(store) == 1
    assert store.find_by_phone("+123") is not None
    assert store.find_by_phone(None) is None
    store.save()
    assert AliasStore(store.path).find_by_name("Axa") is not None


def test_interface_singular_no_chooses_one_alias_of_phone_shared(tmp_path: Path) -> None:
    store = AliasStore(tmp_path / "aliases.json")
    store.register("Axa", [{"person_id": "person_1", "sheets": ["IOL"]}], "+123")
    store.register("Other", [{"person_id": "person_2", "sheets": ["Bull"]}], "+123")
    with pytest.raises(DomainValidationError):
        store.find_by_phone("+123")


@pytest.mark.parametrize("input", [[], None, {"people": "invalid"}, {"people": [None]}])
def test_catalog_legacy_malformed_no_accepts(input: object) -> None:
    with pytest.raises(StructureError):
        catalog_for_matching(input)


@pytest.mark.parametrize(
    "input",
    [
        {"raw_name": "Axa", "last_activity_date": "20261102"},
        {"raw_name": "Axa", "last_activity_date": "secret"},
        {"raw_name": "Axa", "last_activity_date": "2026-11-02", "is_group": "false"},
    ],
)
def test_contact_legacy_requires_date_and_kinds_valid(input: object) -> None:
    with pytest.raises(StructureError) as error:
        contact_from_legacy(input)
    assert "secret" not in str(error.value)


def test_alias_legacy_no_omits_sheets_duplicate() -> None:
    with pytest.raises(StructureError):
        alias_from_legacy("Axa", {"people": [{"person_id": "person_1", "sheets": ["IOL", "IOL"]}]})


def test_error_of_closure_no_hides_error_of_block(
    workbook: RepoXlsx, mocker: MockerFixture
) -> None:
    mocker.patch.object(workbook.reader, "close", side_effect=ExternalServiceError("fake"))
    with pytest.raises(RuntimeError, match="original"), workbook:
        raise RuntimeError("original")


def test_conversion_legacy_preserves_all_the_rows_of_one_person(tmp_path: Path) -> None:
    catalog = {
        "people": [
            {
                "id": "person_1",
                "sorted_name_key": "axa sample",
                "canonical_name": "Axa Sample",
                "holders": ["Axa Sample"],
                "occurrences": [
                    {"sheet": "IOL", "row": 3, "raw_name": "Axa Sample"},
                    {"sheet": "IOL", "row": 8, "raw_name": "Axa Sample"},
                ],
            }
        ]
    }
    results = resolve_contact(
        {"raw_name": "Axa Sample", "last_activity_date": "2026-11-02"},
        build_index(catalog),
        AliasStore(tmp_path / "aliases.json"),
        ExclusionList(tmp_path / "exclusions.json"),
    )
    assert len(results) == 1
    assert results[0]["kind"] == "resolved"
    assert results[0]["locations"] == [
        {"sheet": "IOL", "row": 3},
        {"sheet": "IOL", "row": 8},
    ]
    assert results[0]["row_by_sheet"] == {"IOL": 3}


def test_batch_legacy_invalid_no_learns_aliases_partial(tmp_path: Path) -> None:
    catalog = {
        "people": [
            {
                "id": "person_1",
                "sorted_name_key": "axa sample",
                "canonical_name": "Axa Sample",
                "holders": ["Axa Sample"],
                "occurrences": [{"sheet": "IOL", "row": 3, "raw_name": "Axa Sample"}],
            }
        ]
    }
    aliases = AliasStore(tmp_path / "aliases.json")
    with pytest.raises(StructureError, match="date"):
        resolve_week(
            [
                {"raw_name": "Axa Sample", "last_activity_date": "2026-11-02"},
                {"raw_name": "Other", "last_activity_date": "date_invalid"},
            ],
            catalog,
            aliases,
            ExclusionList(tmp_path / "exclusions.json"),
        )
    assert len(aliases) == 0
    assert not aliases.path.exists()
