"""Superficie pública usada de InstalledAppFlow, contrastada con su implementación."""

from collections.abc import Mapping, Sequence

from google.oauth2.credentials import Credentials
from requests import Session

class InstalledAppFlow:
    oauth2session: Session

    @classmethod
    def from_client_config(
        cls,
        client_config: Mapping[str, object],
        scopes: Sequence[str],
        *,
        autogenerate_code_verifier: bool = False,
    ) -> InstalledAppFlow: ...
    def run_local_server(
        self,
        *,
        host: str = "localhost",
        bind_addr: str | None = None,
        port: int = 8080,
        authorization_prompt_message: str | None = None,
        success_message: str = "",
        open_browser: bool = True,
        timeout_seconds: int | None = None,
    ) -> Credentials: ...
