"""Legacy repository facade backed by the production adapters."""

from client_automation.infra.legacy_repository import (
    LegacyRepository as ClientRepository,
)

__all__ = ["ClientRepository"]
