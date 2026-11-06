"""Legacy alias facade backed by current persistence and reconciliation."""

from pathlib import Path

from client_automation.core.errors import DomainValidationError
from client_automation.infra.json_alias_store import JsonAliasStore
from client_automation.infra.legacy_formats import (
    LegacyAlias,
    LegacyDestination,
    alias_from_legacy,
    alias_to_legacy,
)


class AliasStore:
    """Preserve dictionary queries without duplicating state or conflict rules."""

    def __init__(self, path: str | Path) -> None:
        """Open a typed session without creating a missing file."""
        self.path = Path(path)
        self.store = JsonAliasStore(self.path)

    def find_by_name(self, raw_name: str) -> LegacyAlias | None:
        """Return a separate copy of an exact-name association."""
        alias = self.store.find_by_name(raw_name)
        return None if alias is None else alias_to_legacy(alias)

    def find_by_phone(self, phone: str | None) -> tuple[str, LegacyAlias] | None:
        """Reject shared phone numbers that the singular interface cannot represent."""
        if not phone:
            return None
        aliases = self.store.find_by_phone(phone)
        if len(aliases) > 1:
            raise DomainValidationError(
                "The singular legacy interface cannot choose between shared-phone aliases."
            )
        return (aliases[0].raw_name, alias_to_legacy(aliases[0])) if aliases else None

    def register(
        self, raw_name: str, people: list[LegacyDestination], phone: str | None = None
    ) -> str | None:
        """Reconcile in memory and defer persistence until save is called."""
        return self.store.register(alias_from_legacy(raw_name, {"people": people, "phone": phone}))

    def save(self) -> None:
        """Persist with atomic replacement and external-change checks."""
        self.store.save()

    def __len__(self) -> int:
        """Return the association count without reading the file."""
        return len(self.store)
