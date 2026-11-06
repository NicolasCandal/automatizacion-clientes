"""Legacy facade for the current exclusion adapter."""

from pathlib import Path

from client_automation.infra.json_exclusion_list import JsonExclusionList


class ExclusionList(JsonExclusionList):
    """Accept string or Path inputs and persist additions immediately."""

    def __init__(self, path: str | Path) -> None:
        """Load the legacy representation with strict validation."""
        super().__init__(Path(path))

    def save(self) -> None:
        """Do nothing because additions have already been persisted atomically."""
