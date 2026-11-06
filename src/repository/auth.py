"""Legacy OAuth loading without browser consent or automatic token removal."""

from pathlib import Path

from google.auth.credentials import Credentials

from client_automation.config import Config
from client_automation.core.errors import AuthenticationError as AuthenticationError
from client_automation.infra.google_authentication import GoogleCredentialsProvider


def get_credentials(
    path_credentials: str | Path = Path("secrets/credentials.json"),
    path_token: str | Path = Path("secrets/token.json"),
    *,
    configuration: Config | None = None,
) -> Credentials:
    """Load existing credentials; initial authorization is a separate operation."""
    options = configuration or Config(
        credentials_path=Path(path_credentials), token_path=Path(path_token), _env_file=None
    )
    return GoogleCredentialsProvider(options).load(persist=False)


__all__ = ["AuthenticationError", "get_credentials"]
