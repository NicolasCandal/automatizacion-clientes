"""Validated configuration without side effects on client records."""

from __future__ import annotations

from pathlib import Path
from typing import Final, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .core.models import MatchingPolicy

DEFAULT_ENV_PATH: Final[Path] = Path(".env")

EnvironmentType = Literal["development", "test", "production"]
RepositoryType = Literal["excel", "sheets"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Config(BaseSettings):
    """Validate runtime options from arguments, environment variables, and dotenv."""

    model_config = SettingsConfigDict(
        env_prefix="CA_",
        env_file=DEFAULT_ENV_PATH,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
        validate_default=True,
        str_strip_whitespace=True,
        hide_input_in_errors=True,
    )

    base_directory: Path = Field(default_factory=Path.cwd)
    environment: EnvironmentType = "development"
    log_level: LogLevel = "INFO"
    time_zone: str = "America/Argentina/Buenos_Aires"

    data_directory: Path = Path("data")
    log_directory: Path = Path("logs")
    contacts_path: Path = Path("data/input/week.json")
    whatsapp_manifest_path: Path = Path("data/input/whatsapp/manifest.json")
    excel_path: Path = Path("data/input/clients.xlsx")
    catalog_path: Path = Path("data/state/catalog.json")
    aliases_path: Path = Path("data/state/aliases.json")
    exclusion_list_path: Path = Path("data/state/exclusion_list.json")
    resolution_path: Path = Path("data/output/resolution.json")
    backup_directory: Path = Path("data/backups")

    repository: RepositoryType = "excel"
    spreadsheet_id: str | None = Field(default=None, repr=False)
    credentials_path: Path = Path("secrets/credentials.json")
    token_path: Path = Path("secrets/token.json")
    google_authentication_type: Literal["oauth", "service_account"] = "oauth"
    google_scopes: tuple[str, ...] = ("https://www.googleapis.com/auth/spreadsheets.readonly",)
    google_authorization_timeout_seconds: int = Field(default=180, ge=30, le=600)

    max_attempts: int = Field(default=4, ge=1, le=10)
    retry_min_wait_seconds: float = Field(default=1.0, gt=0, allow_inf_nan=False)
    retry_max_wait_seconds: float = Field(default=30.0, gt=0, allow_inf_nan=False)
    network_timeout_seconds: float = Field(default=30.0, gt=0, allow_inf_nan=False)
    automatic_match_threshold: float = Field(default=92.0, ge=0, le=100, allow_inf_nan=False)
    review_match_threshold: float = Field(default=80.0, ge=0, le=100, allow_inf_nan=False)
    match_suggestion_limit: int = Field(default=3, ge=1)
    dry_run: bool = True

    @field_validator(
        "base_directory",
        "data_directory",
        "log_directory",
        "contacts_path",
        "whatsapp_manifest_path",
        "excel_path",
        "catalog_path",
        "aliases_path",
        "exclusion_list_path",
        "resolution_path",
        "backup_directory",
        "credentials_path",
        "token_path",
        mode="before",
    )
    @classmethod
    def _reject_path_empty(cls, value: object) -> object:
        """Reject empty paths instead of interpreting them as the working directory."""
        if isinstance(value, str):
            if not value.strip():
                raise ValueError("The path cannot be empty.")
            return value.strip()
        return value

    @field_validator("base_directory")
    @classmethod
    def _resolve_directory_base(cls, path: Path) -> Path:
        """Resolve the base directory without creating it or requiring it to exist."""
        return path.expanduser().resolve()

    @field_validator(
        "data_directory",
        "log_directory",
        "contacts_path",
        "whatsapp_manifest_path",
        "excel_path",
        "catalog_path",
        "aliases_path",
        "exclusion_list_path",
        "resolution_path",
        "backup_directory",
        "credentials_path",
        "token_path",
    )
    @classmethod
    def _resolve_path(cls, path: Path, information: ValidationInfo) -> Path:
        """Resolve relative paths against the validated base directory."""
        base_directory = information.data.get("base_directory")
        if not isinstance(base_directory, Path):
            raise ValueError("A valid base_directory is required to resolve paths.")
        path_expanded = path.expanduser()
        if not path_expanded.is_absolute():
            path_expanded = base_directory / path_expanded
        return path_expanded.resolve()

    @field_validator("spreadsheet_id", mode="before")
    @classmethod
    def _normalize_identifier(cls, value: object) -> object:
        """Treat blank identifiers as an unconfigured option."""
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("time_zone")
    @classmethod
    def _validate_zone_time(cls, name: str) -> str:
        """Require a time zone recognized by the IANA database."""
        try:
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("The time zone must be a valid IANA identifier.") from error
        return name

    @model_validator(mode="after")
    def _validate_options_related(self) -> Self:
        """Reject inconsistent options before running the automation."""
        if self.repository == "sheets" and self.spreadsheet_id is None:
            raise ValueError("CA_SPREADSHEET_ID is required when using Google Sheets.")
        if self.retry_min_wait_seconds > self.retry_max_wait_seconds:
            raise ValueError("The minimum retry wait cannot exceed the maximum.")
        if self.credentials_path == self.token_path:
            raise ValueError("Credentials and tokens must be stored in separate files.")
        self.create_matching_policy()
        return self

    @field_validator("google_scopes")
    @classmethod
    def _validate_scopes_google(cls, scopes: tuple[str, ...]) -> tuple[str, ...]:
        """Require explicit, unique Sheets or Drive scopes without whitespace."""
        allowed = {
            "https://www.googleapis.com/auth/spreadsheets.readonly",
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive.readonly",
            "https://www.googleapis.com/auth/drive.file",
            "https://www.googleapis.com/auth/drive",
        }
        if not scopes or len(set(scopes)) != len(scopes) or not set(scopes) <= allowed:
            raise ValueError("Configure unique, supported Google Sheets or Drive scopes.")
        return scopes

    def create_matching_policy(self) -> MatchingPolicy:
        """Build an immutable domain matching policy from the settings."""
        return MatchingPolicy(
            automatic_threshold=self.automatic_match_threshold,
            review_threshold=self.review_match_threshold,
            suggestion_limit=self.match_suggestion_limit,
        )


def load_config(
    path_env: Path | None = DEFAULT_ENV_PATH,
    *,
    base_directory: Path | None = None,
) -> Config:
    """Load fresh configuration without caching or changing the process environment."""
    if base_directory is not None:
        base_explicit = base_directory.expanduser().resolve()
        env_file = path_env.expanduser() if path_env is not None else None
        if env_file is not None and not env_file.is_absolute():
            env_file = base_explicit / env_file
        return Config(base_directory=base_explicit, _env_file=env_file)
    return Config(_env_file=path_env.expanduser() if path_env is not None else None)
