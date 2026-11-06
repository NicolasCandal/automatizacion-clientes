"""WhatsApp import and weekly registration CLI for Excel and Google Sheets."""

import argparse
import logging
from collections.abc import Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import NoReturn

from pydantic import ValidationError
from pydantic_settings import SettingsError

from client_automation.config import Config, load_config
from client_automation.core.contracts import ClientWriter, Clock, RegistrationClientReader
from client_automation.core.errors import (
    AuthenticationError,
    AuthorizationError,
    AutomationError,
    ConcurrencyError,
    ConfigurationError,
    DomainError,
    InfrastructureError,
    RegistrationInterruptedError,
)
from client_automation.core.models import WeeklyPeriod
from client_automation.infra.console_logging import console_logging
from client_automation.infra.excel_client_reader import ExcelClientReader
from client_automation.infra.excel_client_writer import ExcelClientWriter
from client_automation.infra.google_authentication import GoogleCredentialsProvider
from client_automation.infra.json_alias_store import JsonAliasStore
from client_automation.infra.json_catalog_store import JsonCatalogStore
from client_automation.infra.json_contact_reader import JsonContactReader
from client_automation.infra.json_exclusion_list import JsonExclusionList
from client_automation.infra.json_resolution_store import JsonResolutionStore
from client_automation.infra.json_weekly_contacts_store import JsonWeeklyContactsStore
from client_automation.infra.rapidfuzz_comparator import RapidFuzzComparator
from client_automation.infra.sheets_client_reader import create_reader_sheets
from client_automation.infra.sheets_client_writer import (
    WRITE_SCOPES,
    create_writer_sheets,
)
from client_automation.infra.system_clock import SystemClock
from client_automation.infra.whatsapp_txt_extractor import WhatsAppTxtExtractor
from client_automation.services.automation import (
    AutomationResult,
    AutomationService,
)
from client_automation.services.catalog import CatalogService
from client_automation.services.matching import MatchingService
from client_automation.services.registration import RegistrationService
from client_automation.services.whatsapp_import import (
    WhatsAppImportResult,
    WhatsAppImportService,
)

LOGGER = logging.getLogger(__name__)


@dataclass
class _Options(argparse.Namespace):
    header: str = ""
    env_file: Path | None = None
    no_env: bool = False
    base_directory: Path | None = None
    dry_run: bool | None = None
    allow_pending: bool = False
    rebuild_catalog: bool = False
    help: bool = False
    authorize_google: bool = False
    import_whatsapp: bool = False
    start_date: date | None = None
    end_date: date | None = None
    manifest: Path | None = None


class _DryRunWriter:
    """Prevent writes through the Sheets dry-run port without accessing resources."""

    def create_backup(self) -> Path:
        raise ConfigurationError("A dry run cannot create backups.")

    def apply_marks(
        self,
        sheet: str,
        column: int,
        header: str,
        rows: frozenset[int],
        *,
        expected_revision: str,
    ) -> None:
        raise ConfigurationError("A dry run cannot apply marks.")


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        """Report argument errors without printing or exposing unknown arguments."""
        raise ConfigurationError("Invalid arguments. See --help.")


def _create_parser() -> _ArgumentParser:
    parser = _ArgumentParser(
        prog="client-automation",
        add_help=False,
        allow_abbrev=False,
        exit_on_error=False,
        description="Import WhatsApp TXT chats or register contacts in Excel or Google Sheets.",
    )
    parser.add_argument(
        "-h",
        "--help",
        dest="help",
        action="store_true",
        help="Show help without loading files.",
    )
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument(
        "--authorize-google",
        action="store_true",
        help="Authorize OAuth in a browser and save the token without running registration.",
    )
    operation.add_argument(
        "--import-whatsapp",
        action="store_true",
        help="Generate weekly input from TXT exports without registering marks.",
    )
    parser.add_argument(
        "--start-date",
        type=_date_argument,
        help="Inclusive start date YYYY-MM-DD; required for WhatsApp import.",
    )
    parser.add_argument(
        "--end-date",
        type=_date_argument,
        help="Inclusive end date YYYY-MM-DD; required for WhatsApp import.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        help="Import manifest; overrides CA_WHATSAPP_MANIFEST_PATH.",
    )
    parser.add_argument(
        "--week-header",
        dest="header",
        default="",
        help="Week identifier; use a label including its year or an exact existing header.",
    )
    environment = parser.add_mutually_exclusive_group()
    environment.add_argument(
        "--env-file",
        dest="env_file",
        type=Path,
        default=None,
        help="Configuration file; defaults to .env.",
    )
    environment.add_argument(
        "--no-env",
        action="store_true",
        help="Use environment variables and defaults without loading a dotenv file.",
    )
    parser.add_argument(
        "--base-directory",
        type=Path,
        help="Base directory for configuration and relative paths.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_const",
        const=True,
        default=None,
        help="Run the workflow without saving files or changing marks.",
    )
    mode.add_argument(
        "--apply",
        dest="dry_run",
        action="store_const",
        const=False,
        help="Save imported input or register marks with a backup, depending on the operation.",
    )
    parser.add_argument(
        "--allow-pending",
        action="store_true",
        help="Allow a partial replacement using only resolved contacts.",
    )
    parser.add_argument(
        "--rebuild-catalog",
        action="store_true",
        help="Rebuild the catalog even when its cache is invalid.",
    )
    return parser


def _date_argument(value: str) -> date:
    """Require canonical dates before loading settings or opening input files."""
    try:
        parsed_date = date.fromisoformat(value)
    except ValueError as cause:
        raise argparse.ArgumentTypeError("Use a valid YYYY-MM-DD date.") from cause
    if parsed_date.isoformat() != value:
        raise argparse.ArgumentTypeError("Use exactly the YYYY-MM-DD format.")
    return parsed_date


def _validate_options(options: _Options) -> WeeklyPeriod | None:
    """Validate authorization, import, and registration options without I/O."""
    if not options.import_whatsapp and (
        options.start_date is not None
        or options.end_date is not None
        or options.manifest is not None
    ):
        raise ConfigurationError("Dates and a manifest require --import-whatsapp.")
    if options.authorize_google:
        if (
            options.header
            or options.dry_run is not None
            or options.allow_pending
            or options.rebuild_catalog
        ):
            raise ConfigurationError("Google authorization does not accept registration options.")
        return None
    if options.import_whatsapp:
        if options.header or options.allow_pending or options.rebuild_catalog:
            raise ConfigurationError("Import does not accept registration options.")
        if options.start_date is None or options.end_date is None:
            raise ConfigurationError("Import requires --start-date and --end-date.")
        return WeeklyPeriod(start_date=options.start_date, end_date=options.end_date)
    if not options.header.strip():
        raise ConfigurationError("Indique --week-header with the week that desea register.")
    return None


def validate_paths(configuration: Config, *, env_file: Path | None = None) -> None:
    """Reject overlapping or nested paths before constructing adapters."""
    files: tuple[Path, ...] = (
        configuration.excel_path,
        configuration.contacts_path,
        configuration.whatsapp_manifest_path,
        configuration.catalog_path,
        configuration.aliases_path,
        configuration.exclusion_list_path,
        configuration.resolution_path,
        configuration.credentials_path,
        configuration.token_path,
    )
    if env_file is not None:
        files = (*files, env_file.expanduser().resolve())
    for catalog_index, path in enumerate(files):
        if any(
            path.is_relative_to(other) or other.is_relative_to(path)
            for other in files[catalog_index + 1 :]
        ):
            raise ConfigurationError("File paths must be distinct and must not be nested.")
        if configuration.backup_directory.is_relative_to(path):
            raise ConfigurationError("The backup directory cannot occupy a file path.")


def run_excel(
    configuration: Config,
    *,
    header: str,
    dry_run: bool | None = None,
    allow_pending: bool = False,
    force_catalog: bool = False,
) -> AutomationResult:
    """Compose Excel adapters and services, then close the reader."""
    if configuration.repository != "excel":
        raise ConfigurationError("Este punto of input requires the repository Excel.")
    if not header.strip():
        raise ConfigurationError("Indique the header of the week.")
    validate_paths(configuration)
    clock = SystemClock()
    with ExcelClientReader(configuration.excel_path) as reader:
        return _run_with_adapters(
            configuration,
            reader=reader,
            writer=ExcelClientWriter(
                configuration.excel_path,
                backup_directory=configuration.backup_directory,
                clock=clock,
            ),
            clock=clock,
            source=configuration.excel_path.as_uri(),
            header=header,
            dry_run=configuration.dry_run if dry_run is None else dry_run,
            allow_pending=allow_pending,
            force_catalog=force_catalog,
        )


def run_sheets(
    configuration: Config,
    *,
    header: str,
    dry_run: bool | None = None,
    allow_pending: bool = False,
    force_catalog: bool = False,
) -> AutomationResult:
    """Run the shared Google workflow and close sessions even after failures."""
    if configuration.repository != "sheets" or configuration.spreadsheet_id is None:
        raise ConfigurationError("Este punto of input requires one document of Google Sheets.")
    if not header.strip():
        raise ConfigurationError("Indique the header of the week.")
    validate_paths(configuration)
    dry_run = configuration.dry_run if dry_run is None else dry_run
    if not dry_run and not WRITE_SCOPES.intersection(configuration.google_scopes):
        raise ConfigurationError("Configure a write scope before applying changes to Sheets.")
    credentials = GoogleCredentialsProvider(configuration).load(persist=not dry_run)
    clock = SystemClock()
    source = f"sheets:{configuration.spreadsheet_id}"
    with ExitStack() as resources:
        reader = resources.enter_context(create_reader_sheets(configuration, credentials))
        writer: ClientWriter = _DryRunWriter()
        if not dry_run:
            writer = resources.enter_context(
                create_writer_sheets(configuration, credentials, clock=clock)
            )
        return _run_with_adapters(
            configuration,
            reader=reader,
            writer=writer,
            clock=clock,
            source=source,
            header=header,
            dry_run=dry_run,
            allow_pending=allow_pending,
            force_catalog=force_catalog,
        )


def authorize_google(configuration: Config) -> None:
    """Start explicit OAuth consent and save the token without reading contacts."""
    if configuration.repository != "sheets":
        raise ConfigurationError("Google authorization requires the Sheets repository.")
    validate_paths(configuration)
    GoogleCredentialsProvider(configuration).authorize()
    LOGGER.info("Google authorized; contact registration has not been run.")


def run_import_operation(
    configuration: Config,
    *,
    period: WeeklyPeriod,
    dry_run: bool | None = None,
) -> WhatsAppImportResult:
    """Compose local chat import and generate only the weekly input document."""
    validate_paths(configuration)
    service = WhatsAppImportService(
        extractor=WhatsAppTxtExtractor(configuration.whatsapp_manifest_path),
        store=JsonWeeklyContactsStore(
            configuration.contacts_path,
            manifest=configuration.whatsapp_manifest_path,
        ),
        clock=SystemClock(),
    )
    return service.run(
        period,
        dry_run=configuration.dry_run if dry_run is None else dry_run,
    )


def _run_with_adapters(
    configuration: Config,
    *,
    reader: RegistrationClientReader,
    writer: ClientWriter,
    clock: Clock,
    source: str,
    header: str,
    dry_run: bool,
    allow_pending: bool,
    force_catalog: bool,
) -> AutomationResult:
    """Compose the same domain services for either supported source."""
    catalogs = JsonCatalogStore(configuration.catalog_path)
    reports = JsonResolutionStore(configuration.resolution_path)
    contacts = JsonContactReader(configuration.contacts_path)
    flow = AutomationService(
        catalog=CatalogService(
            reader_clients=reader,
            store_catalog=catalogs,
            clock=clock,
            source_id=source,
        ),
        matching=MatchingService(
            reader_contacts=contacts,
            store_catalog=catalogs,
            store_aliases=JsonAliasStore(configuration.aliases_path),
            list_exclusion=JsonExclusionList(configuration.exclusion_list_path),
            store_resolutions=reports,
            comparator=RapidFuzzComparator(),
            policy=configuration.create_matching_policy(),
        ),
        registration=RegistrationService(
            reader_clients=reader,
            writer_clients=writer,
            source_id=source,
        ),
        reader_contacts=contacts,
        store_catalog=catalogs,
        store_resolutions=reports,
    )
    return flow.run(
        header=header,
        dry_run=dry_run,
        allow_pending=allow_pending,
        force_catalog=force_catalog,
    )


def main(arguments: Sequence[str] | None = None) -> int:
    """Run the CLI and return an exit code without exposing secrets."""
    with console_logging() as package:
        try:
            parser = _create_parser()
            options = _Options()
            parser.parse_args(arguments, namespace=options)
            if options.help:
                LOGGER.info("%s", parser.format_help().rstrip())
                return 0
            period = _validate_options(options)
            path_env = None if options.no_env else (options.env_file or Path(".env"))
            path_env_resolved = path_env.expanduser() if path_env is not None else None
            if path_env_resolved is not None and options.base_directory is not None:
                path_env_resolved = (
                    options.base_directory.expanduser().resolve() / path_env_resolved
                )
            if options.env_file is not None and (
                path_env_resolved is None or not path_env_resolved.is_file()
            ):
                raise ConfigurationError("The file specified by --env-file must exist.")
            configuration = load_config(path_env, base_directory=options.base_directory)
            if options.manifest is not None:
                manifest = options.manifest.expanduser()
                if not manifest.is_absolute():
                    manifest = configuration.base_directory / manifest
                configuration = configuration.model_copy(
                    update={"whatsapp_manifest_path": manifest.resolve()}
                )
            validate_paths(configuration, env_file=path_env_resolved)
            package.setLevel(configuration.log_level)
            if period is not None:
                import_operation = run_import_operation(
                    configuration, period=period, dry_run=options.dry_run
                )
                LOGGER.info(
                    "Weekly input prepared: dry_run=%s; %s chats; %s contacts.",
                    import_operation.dry_run,
                    import_operation.extraction.total_chats_scanned,
                    len(import_operation.extraction.week.contacts),
                )
                return 0
            if options.authorize_google:
                authorize_google(configuration)
                return 0
            run = run_excel if configuration.repository == "excel" else run_sheets
            result = run(
                configuration,
                header=options.header.strip(),
                dry_run=options.dry_run,
                allow_pending=options.allow_pending,
                force_catalog=options.rebuild_catalog,
            )
            if result.registration is None:
                return 3
            LOGGER.info(
                "Workflow completed: dry_run=%s; %s sheets applied.",
                result.dry_run,
                len(result.registration.applied_sheets),
            )
            return 0
        except (argparse.ArgumentError, ValidationError, SettingsError):
            LOGGER.error("[CONFIGURATION] Check the arguments and CA_ environment variables.")
            return 2
        except RegistrationInterruptedError as cause:
            LOGGER.error(
                "[%s] Registration stopped: %s confirmed sheets; sheet in progress=%s. "
                "Check the configured backup.",
                cause.CODE,
                len(cause.confirmed_sheets),
                cause.current_sheet is not None,
            )
            return 6
        except ConcurrencyError:
            LOGGER.error("[CONCURRENCY] The source changed. Refresh the inputs before retrying.")
            return 5
        except (ConfigurationError, DomainError) as cause:
            LOGGER.error(
                "[%s] The input does not permit this operation. "
                "See --help and check the configuration.",
                cause.CODE,
            )
            return 2
        except (AuthenticationError, AuthorizationError) as cause:
            LOGGER.error(
                "[%s] Check Google credentials and permissions. "
                "Use --authorize-google as a separate operation for OAuth consent.",
                cause.CODE,
            )
            return 4
        except (InfrastructureError, OSError):
            LOGGER.error(
                "[INFRASTRUCTURE] A resource could not be accessed or saved. "
                "Auxiliary files may already have been saved."
            )
            return 4
        except AutomationError as cause:
            LOGGER.error(
                "[%s] The workflow could not complete. Review auxiliary files before retrying.",
                cause.CODE,
            )
            return 1
        except KeyboardInterrupt:
            LOGGER.warning(
                "Execution interrupted. Review the source and backups before continuing."
            )
            return 130
        except Exception:
            LOGGER.error(
                "[UNEXPECTED] The workflow stopped. Review the state and backups before retrying."
            )
            return 1
