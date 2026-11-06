"""Isolated settings tests without credentials or service access."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from client_automation.config import Config, load_config
from client_automation.core.models import MatchingPolicy


@pytest.fixture(autouse=True)
def isolate_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Regression tests for config."""
    from os import environ

    for name in tuple(environ):
        if name.upper().startswith("CA_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)


def test_values_by_default_are_safe(tmp_path: Path) -> None:
    configuration = load_config(path_env=None)

    assert configuration.repository == "excel"
    assert configuration.dry_run is True
    assert configuration.spreadsheet_id is None
    assert configuration.excel_path == tmp_path / "data/input/clients.xlsx"
    assert configuration.resolution_path == tmp_path / "data/output/resolution.json"
    assert configuration.credentials_path == tmp_path / "secrets/credentials.json"
    assert configuration.time_zone == "America/Argentina/Buenos_Aires"
    assert not (tmp_path / "data").exists()
    assert not (tmp_path / "secrets").exists()
    assert not (tmp_path / "logs").exists()


def test_environment_overrides_over_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("CA_LOG_LEVEL=WARNING\nCA_DRY_RUN=false\n", encoding="utf-8")
    monkeypatch.setenv("CA_LOG_LEVEL", "ERROR")

    configuration = load_config()

    assert configuration.log_level == "ERROR"
    assert configuration.dry_run is False


def test_argument_explicit_overrides_over_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CA_LOG_LEVEL", "ERROR")

    assert Config(log_level="DEBUG", _env_file=None).log_level == "DEBUG"


def test_can_disable_dotenv(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("CA_REPOSITORY=invalid\n", encoding="utf-8")

    assert load_config(path_env=None).repository == "excel"


def test_dotenv_can_share_with_the_application_previous(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "SPREADSHEET_ID=identifier_previous\nCA_LOG_LEVEL=WARNING\n", encoding="utf-8"
    )

    configuration = load_config()

    assert configuration.spreadsheet_id is None
    assert configuration.log_level == "WARNING"


def test_base_explicit_resolves_file_env_and_paths(tmp_path: Path) -> None:
    base_configured = tmp_path / "other_base"
    base_configured.mkdir()
    (base_configured / "options.env").write_text(
        "CA_EXCEL_PATH=input/clients.xlsx\n", encoding="utf-8"
    )

    configuration = load_config(Path("options.env"), base_directory=base_configured)

    assert configuration.excel_path == base_configured / "input/clients.xlsx"
    assert configuration.base_directory == base_configured


def test_base_of_environment_resolves_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    base_configured = tmp_path / "base_start_date_environment"
    monkeypatch.setenv("CA_BASE_DIRECTORY", str(base_configured))

    assert load_config(path_env=None).excel_path == (base_configured / "data/input/clients.xlsx")


def test_base_explicit_overrides_over_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CA_BASE_DIRECTORY", str(tmp_path / "base_of_environment"))

    configuration = load_config(path_env=None, base_directory=tmp_path / "base_explicit")

    assert configuration.base_directory == tmp_path / "base_explicit"


def test_path_absolute_no_reinterprets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path_absolute = tmp_path / "location_independent/clients.xlsx"
    monkeypatch.setenv("CA_EXCEL_PATH", str(path_absolute))

    configuration = load_config(path_env=None, base_directory=tmp_path / "other_base")

    assert configuration.excel_path == path_absolute


@pytest.mark.parametrize("identifier", ["", "   "])
def test_sheets_requires_identifier_no_empty(
    monkeypatch: pytest.MonkeyPatch, identifier: str
) -> None:
    monkeypatch.setenv("CA_REPOSITORY", "sheets")
    monkeypatch.setenv("CA_SPREADSHEET_ID", identifier)

    with pytest.raises(ValidationError, match="CA_SPREADSHEET_ID"):
        load_config(path_env=None)


def test_sheets_requires_identifier_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CA_REPOSITORY", "sheets")

    with pytest.raises(ValidationError, match="CA_SPREADSHEET_ID"):
        load_config(path_env=None)


def test_sheets_no_opens_credentials_on_load(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CA_REPOSITORY", "sheets")
    monkeypatch.setenv("CA_SPREADSHEET_ID", "  identifier_of_sample  ")

    configuration = load_config(path_env=None)

    assert configuration.spreadsheet_id == "identifier_of_sample"
    assert not configuration.credentials_path.exists()
    assert "identifier_of_sample" not in repr(configuration)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CA_MAX_ATTEMPTS", "0"),
        ("CA_MAX_ATTEMPTS", "11"),
        ("CA_NETWORK_TIMEOUT_SECONDS", "0"),
        ("CA_NETWORK_TIMEOUT_SECONDS", "nan"),
        ("CA_NETWORK_TIMEOUT_SECONDS", "inf"),
        ("CA_RETRY_MIN_WAIT_SECONDS", "-1"),
        ("CA_RETRY_MAX_WAIT_SECONDS", "inf"),
        ("CA_REPOSITORY", "nonexistent"),
        ("CA_LOG_LEVEL", "INVALID"),
        ("CA_ENVIRONMENT", "invalid"),
        ("CA_DRY_RUN", "maybe"),
        ("CA_TIME_ZONE", "Zone/Nonexistent"),
        ("CA_EXCEL_PATH", "   "),
        ("CA_RESOLUTION_PATH", "   "),
        ("CA_BASE_DIRECTORY", ""),
    ],
)
def test_rejects_options_invalid(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        load_config(path_env=None)


def test_rejects_waits_reversed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CA_RETRY_MIN_WAIT_SECONDS", "60")
    monkeypatch.setenv("CA_RETRY_MAX_WAIT_SECONDS", "30")

    with pytest.raises(ValidationError, match="minimum retry wait"):
        load_config(path_env=None)


def test_token_no_can_overwrite_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CA_CREDENTIALS_PATH", "secrets/same_file.json")
    monkeypatch.setenv("CA_TOKEN_PATH", "secrets/same_file.json")

    with pytest.raises(ValidationError, match="separate files"):
        load_config(path_env=None)


def test_configuration_is_immutable() -> None:
    configuration = load_config(path_env=None)

    with pytest.raises(ValidationError, match="frozen"):
        configuration.__setattr__("dry_run", False)


def test_path_of_report_configures_start_date_environment_and_base(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CA_RESOLUTION_PATH", "reports/result.json")
    base = tmp_path / "base_configured"

    configuration = load_config(path_env=None, base_directory=base)

    assert configuration.resolution_path == base / "reports/result.json"
    assert not base.exists()


def test_loads_successive_no_reuse_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    assert load_config(path_env=None).log_level == "INFO"
    monkeypatch.setenv("CA_LOG_LEVEL", "DEBUG")

    assert load_config(path_env=None).log_level == "DEBUG"


def test_policy_default_matches_with_the_domain() -> None:
    assert load_config(path_env=None).create_matching_policy() == MatchingPolicy()


def test_policy_can_configure_start_date_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CA_AUTOMATIC_MATCH_THRESHOLD", "98")
    monkeypatch.setenv("CA_REVIEW_MATCH_THRESHOLD", "90")
    monkeypatch.setenv("CA_MATCH_SUGGESTION_LIMIT", "5")

    assert load_config(path_env=None).create_matching_policy() == MatchingPolicy(
        automatic_threshold=98.0, review_threshold=90.0, suggestion_limit=5
    )


@pytest.mark.parametrize(
    ("automatic", "revision", "limit"),
    [
        ("80", "80", "3"),
        ("80", "92", "3"),
        ("101", "80", "3"),
        ("nan", "80", "3"),
        ("92", "-1", "3"),
        ("92", "80", "0"),
    ],
)
def test_configuration_rejects_policy_inconsistent(
    monkeypatch: pytest.MonkeyPatch, automatic: str, revision: str, limit: str
) -> None:
    monkeypatch.setenv("CA_AUTOMATIC_MATCH_THRESHOLD", automatic)
    monkeypatch.setenv("CA_REVIEW_MATCH_THRESHOLD", revision)
    monkeypatch.setenv("CA_MATCH_SUGGESTION_LIMIT", limit)

    with pytest.raises(ValidationError):
        load_config(path_env=None)
