"""Synthetic chat import, atomic output, and Excel workflow integration."""

import json
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, date, datetime
from io import BytesIO
from os import environ
from pathlib import Path
from typing import cast

import pytest
from openpyxl import Workbook
from openpyxl.worksheet.worksheet import Worksheet
from pytest_mock import MockerFixture

from client_automation.config import Config, load_config
from client_automation.core.contracts import ContactExtractor, WeeklyContactsStore
from client_automation.core.errors import (
    ConcurrencyError,
    ConfigurationError,
    DomainValidationError,
    PersistenceError,
    StructureError,
)
from client_automation.core.models import WeeklyPeriod
from client_automation.core.whatsapp_import import parse_activity
from client_automation.infra import json_weekly_contacts_store as store_module
from client_automation.infra import whatsapp_txt_extractor as module
from client_automation.infra.json_contact_reader import JsonContactReader
from client_automation.infra.json_weekly_contacts_store import JsonWeeklyContactsStore
from client_automation.infra.whatsapp_txt_extractor import WhatsAppTxtExtractor
from client_automation.main import run_excel, validate_paths
from client_automation.services.whatsapp_import import WhatsAppImportService

type Object = dict[str, object]
PERIOD = WeeklyPeriod(start_date=date(2026, 11, 2), end_date=date(2026, 11, 6))


class FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 11, 6, 18, tzinfo=UTC)


@pytest.fixture(autouse=True)
def isolate_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(environ):
        if name.upper().startswith("CA_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def configuration(tmp_path: Path) -> Config:
    configuration = Config(base_directory=tmp_path, _env_file=None)
    folder = configuration.whatsapp_manifest_path.parent
    folder.mkdir(parents=True)
    (folder / "axa.txt").write_text(
        "02/11/2026, 10:00 - Axa: private content one\n"
        "05/11/2026, 10:00 - Me: private content two\n"
        "10/11/2026, 10:00 - Axa: after the period\n",
        encoding="utf-8",
    )
    (folder / "group.txt").write_bytes(
        "\ufeff[03/11/2026, 8:00:30 p. m.] Me: group message\r\n".encode()
    )
    (folder / "inactive.txt").write_text(
        "01/11/2026, 10:00 - Notuka: before the period\n", encoding="utf-8"
    )
    write_manifest(
        configuration,
        {
            "schema_version": 1,
            "date_order": "day_month",
            "chats": [
                {
                    "file": "axa.txt",
                    "raw_name": "Axa Sample (IOL)",
                    "phone": "+5491100000000",
                    "is_group": False,
                    "senders": ["Me", "Axa"],
                },
                {
                    "file": "group.txt",
                    "raw_name": "Team Sample",
                    "is_group": True,
                    "senders": ["Me"],
                },
                {
                    "file": "inactive.txt",
                    "raw_name": "Notuka Example",
                    "is_group": False,
                    "senders": ["Notuka"],
                },
            ],
        },
    )
    return configuration


def write_manifest(configuration: Config, document: object) -> None:
    configuration.whatsapp_manifest_path.write_text(json.dumps(document), encoding="utf-8")


def read_manifest(configuration: Config) -> Object:
    return cast(
        Object, json.loads(configuration.whatsapp_manifest_path.read_text(encoding="utf-8"))
    )


def inputs(document: Object) -> list[Object]:
    return cast(list[Object], document["chats"])


def service(configuration: Config) -> WhatsAppImportService:
    extractor: ContactExtractor = WhatsAppTxtExtractor(configuration.whatsapp_manifest_path)
    store: WeeklyContactsStore = JsonWeeklyContactsStore(
        configuration.contacts_path, manifest=configuration.whatsapp_manifest_path
    )
    return WhatsAppImportService(extractor=extractor, store=store, clock=FixedClock())


def files(base: Path) -> dict[Path, bytes]:
    return {path.relative_to(base): path.read_bytes() for path in base.rglob("*") if path.is_file()}


def test_dry_run_validates_and_no_saves_files(configuration: Config) -> None:
    original = files(configuration.base_directory)
    result = service(configuration).run(PERIOD)
    assert result.dry_run
    assert result.extraction.total_chats_scanned == 3
    assert result.extraction.recognized_messages == 5
    assert len(result.extraction.week.contacts) == 2
    assert result.extraction.week.contacts[0].last_activity_date == date(2026, 11, 5)
    assert result.extraction.week.contacts[1].is_group
    assert files(configuration.base_directory) == original


def test_output_is_compatible_with_reader_and_no_contains_messages(
    configuration: Config,
    caplog: pytest.LogCaptureFixture,
) -> None:
    original = files(configuration.whatsapp_manifest_path.parent)
    with caplog.at_level("INFO"):
        result = service(configuration).run(PERIOD, dry_run=False)
    assert not result.dry_run
    assert JsonContactReader(configuration.contacts_path).read() == result.extraction.week
    content = configuration.contacts_path.read_text(encoding="utf-8")
    assert "content private" not in content
    assert "group message" not in content
    assert "after the period" not in content
    assert "Axa Sample" not in caplog.text
    assert "+54911" not in caplog.text
    assert str(configuration.whatsapp_manifest_path) not in caplog.text
    assert files(configuration.whatsapp_manifest_path.parent) == original


def test_input_imported_feeds_engine_and_excludes_groups(configuration: Config) -> None:
    service(configuration).run(PERIOD, dry_run=False)
    workbook = Workbook()
    initial = workbook.active
    assert isinstance(initial, Worksheet)
    workbook.remove(initial)
    for name in ("Inviu", "IOL", "Bull", "Balanz", "PPI"):
        destination = workbook.create_sheet(name)
        destination.cell(1, 1, "Client")
        destination.cell(2, 4 if name == "Balanz" else 3, "Week")
        destination.cell(3, 1, "Axa Sample")
    workbook.save(configuration.excel_path)
    workbook.close()
    original = files(configuration.base_directory)
    result = run_excel(configuration, header="Week")
    assert len(result.resolution.resolved) == 1
    assert len(result.resolution.ignored) == 1
    assert result.resolution.resolved[0].locations[0].sheet == "IOL"
    assert files(configuration.base_directory) == original


def test_one_file_invalid_preserves_output_previous(configuration: Config) -> None:
    configuration.contacts_path.write_bytes(b"salida anterior")
    (configuration.whatsapp_manifest_path.parent / "group.txt").write_bytes(b"invalido")
    with pytest.raises(StructureError):
        service(configuration).run(PERIOD, dry_run=False)
    assert configuration.contacts_path.read_bytes() == b"salida anterior"


def test_error_of_saved_no_retries_as_import_operation_complete(
    configuration: Config,
    mocker: MockerFixture,
) -> None:
    configuration.contacts_path.write_bytes(b"salida anterior")
    extract = mocker.spy(WhatsAppTxtExtractor, "extract")
    save = mocker.patch.object(store_module, "save_json", side_effect=PersistenceError("fake"))
    with pytest.raises(PersistenceError):
        service(configuration).run(PERIOD, dry_run=False)
    extract.assert_called_once()
    save.assert_called_once()
    assert configuration.contacts_path.read_bytes() == b"salida anterior"


@pytest.mark.parametrize(
    "document", [[], {}, {"schema_version": True, "date_order": "day_month", "chats": []}]
)
def test_manifest_incompatible_rejects(configuration: Config, document: object) -> None:
    write_manifest(configuration, document)
    with pytest.raises(StructureError):
        service(configuration).run(PERIOD)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 2),
        ("date_order", "automatic"),
        ("short_year_century", True),
        ("chats", []),
        ("chats", "secret"),
        ("unknown", 3),
    ],
)
def test_fields_invalid_of_manifest_no_generate_output(
    configuration: Config,
    field: str,
    value: object,
) -> None:
    document = read_manifest(configuration)
    document[field] = value
    write_manifest(configuration, document)
    with pytest.raises(StructureError):
        service(configuration).run(PERIOD)
    assert not configuration.contacts_path.exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("file", "../outside.txt"),
        ("file", "chat.zip"),
        ("file", "chat.txt:hidden.txt"),
        ("file", "chat\u0000.txt"),
        ("file", ""),
        ("raw_name", 42),
        ("is_group", "false"),
        ("phone", 123),
        ("senders", []),
        ("senders", [5]),
        ("unknown", None),
    ],
)
def test_fields_invalid_of_chat_reject_before_of_read_txt(
    configuration: Config,
    field: str,
    value: object,
    mocker: MockerFixture,
) -> None:
    document = read_manifest(configuration)
    inputs(document)[0][field] = value
    write_manifest(configuration, document)
    read = mocker.spy(WhatsAppTxtExtractor, "_lines")
    with pytest.raises(StructureError):
        service(configuration).run(PERIOD)
    read.assert_not_called()


@pytest.mark.parametrize("case", ["name", "phone", "duplicates", "sender_white"])
def test_identities_and_senders_empty_no_omit(configuration: Config, case: str) -> None:
    document = read_manifest(configuration)
    chat = inputs(document)[0]
    if case == "name":
        chat.update(raw_name="", phone=None)
    elif case == "phone":
        chat["phone"] = " "
    else:
        chat["senders"] = ["Me", "Me"] if case == "duplicates" else [" "]
    write_manifest(configuration, document)
    with pytest.raises(DomainValidationError):
        service(configuration).run(PERIOD)


def test_paths_repeated_and_absolute_reject(configuration: Config) -> None:
    original = read_manifest(configuration)
    duplicate = deepcopy(original)
    inputs(duplicate).append(deepcopy(inputs(duplicate)[0]))
    write_manifest(configuration, duplicate)
    with pytest.raises(StructureError):
        service(configuration).run(PERIOD)
    inputs(original)[0]["file"] = str(configuration.whatsapp_manifest_path.parent / "axa.txt")
    write_manifest(configuration, original)
    with pytest.raises(StructureError):
        service(configuration).run(PERIOD)


@pytest.mark.parametrize(
    "content", [b'{"chats": [], "chats": []}', b"{invalido", b"\xff", b" " * (1024 * 1024 + 1)]
)
def test_json_invalid_duplicate_or_oversized_rejects(configuration: Config, content: bytes) -> None:
    configuration.whatsapp_manifest_path.write_bytes(content)
    with pytest.raises(StructureError):
        service(configuration).run(PERIOD)


@pytest.mark.parametrize(
    "options",
    [
        {"limit_chats": 1},
        {"limit_file_bytes": 10},
        {"limit_total_bytes": 10},
        {"limit_line_bytes": 10},
    ],
)
def test_limits_no_truncate_inputs(configuration: Config, options: dict[str, int]) -> None:
    with pytest.raises(StructureError):
        WhatsAppTxtExtractor(configuration.whatsapp_manifest_path, **options).extract(PERIOD)


@pytest.mark.parametrize("limit", [0, -1, True])
def test_limits_invalid_no_open_files(tmp_path: Path, limit: int) -> None:
    with pytest.raises(ConfigurationError):
        WhatsAppTxtExtractor(tmp_path / "missing.json", limit_chats=limit)


@pytest.mark.parametrize("case", ["missing", "directory", "utf8"])
def test_errors_of_input_translate(configuration: Config, case: str) -> None:
    path = configuration.whatsapp_manifest_path.parent / "axa.txt"
    path.unlink()
    if case == "directory":
        path.mkdir()
    elif case == "utf8":
        path.write_bytes(b"\xff\xfe")
    with pytest.raises(PersistenceError if case == "missing" else StructureError):
        service(configuration).run(PERIOD)


@pytest.mark.parametrize("change_manifest", [False, True])
def test_changes_during_read_prevent_save(
    configuration: Config,
    mocker: MockerFixture,
    change_manifest: bool,
) -> None:
    parse = parse_activity
    change = False

    def parse_and_change(*arguments: object, **options: object) -> object:
        nonlocal change
        result = cast(Callable[..., object], parse)(*arguments, **options)
        if not change:
            path = (
                configuration.whatsapp_manifest_path
                if change_manifest
                else configuration.whatsapp_manifest_path.parent / "axa.txt"
            )
            path.write_bytes(path.read_bytes() + b"\n")
            change = True
        return result

    mocker.patch.object(module, "parse_activity", side_effect=parse_and_change)
    with pytest.raises(ConcurrencyError):
        service(configuration).run(PERIOD, dry_run=False)
    assert not configuration.contacts_path.exists()


def test_file_closes_if_fails_interpretation(configuration: Config, mocker: MockerFixture) -> None:
    open = Path.open
    flow = BytesIO(b"32/11/2026, 10:00 - Axa: secreto\n")
    path_chat = configuration.whatsapp_manifest_path.parent / "axa.txt"

    def open_simulated(self: Path, *arguments: object, **options: object) -> object:
        if self == path_chat:
            return flow
        return cast(Callable[..., object], open)(self, *arguments, **options)

    mocker.patch.object(Path, "open", autospec=True, side_effect=open_simulated)
    with pytest.raises(DomainValidationError):
        service(configuration).run(PERIOD)
    assert flow.closed


def test_destination_no_can_be_txt_or_the_manifest(configuration: Config) -> None:
    for destination in (
        configuration.whatsapp_manifest_path,
        configuration.whatsapp_manifest_path.parent / "axa.txt",
    ):
        with pytest.raises(ConfigurationError):
            JsonWeeklyContactsStore(destination, manifest=configuration.whatsapp_manifest_path)


def test_clock_without_zone_no_saves(configuration: Config, mocker: MockerFixture) -> None:
    clock = mocker.Mock()
    clock.now.return_value = datetime(2026, 11, 6)
    store = mocker.Mock()
    importer = WhatsAppImportService(
        extractor=WhatsAppTxtExtractor(configuration.whatsapp_manifest_path),
        store=store,
        clock=clock,
    )
    with pytest.raises(ConfigurationError):
        importer.run(PERIOD, dry_run=False)
    store.save.assert_not_called()


def test_path_new_configures_and_protects_without_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CA_WHATSAPP_MANIFEST_PATH", "exports/batch.json")
    configuration = load_config(path_env=None, base_directory=tmp_path)
    assert configuration.whatsapp_manifest_path == tmp_path / "exports/batch.json"
    assert not configuration.whatsapp_manifest_path.parent.exists()
    with pytest.raises(ConfigurationError):
        validate_paths(
            configuration.model_copy(update={"whatsapp_manifest_path": configuration.contacts_path})
        )
