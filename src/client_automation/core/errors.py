"""Application exceptions independent of external clients and services."""

from pathlib import Path
from typing import ClassVar


class AutomationError(Exception):
    """Base class for expected failures handled by application orchestration."""

    CODE: ClassVar[str] = "AUTOMATION"


class DomainError(AutomationError):
    """A business rule or state condition prevents the operation."""

    CODE: ClassVar[str] = "DOMAIN"


class DomainValidationError(DomainError, ValueError):
    """A value or relationship violates a domain model invariant."""

    CODE: ClassVar[str] = "VALIDATION"


class StructureError(DomainError):
    """The source lacks the required sheets, columns, or structure."""

    CODE: ClassVar[str] = "STRUCTURE"


class ConcurrencyError(DomainError):
    """The source changed and must be read again before planning a write."""

    CODE: ClassVar[str] = "CONCURRENCY"


class ConfigurationError(AutomationError):
    """Runtime configuration must be corrected before continuing."""

    CODE: ClassVar[str] = "CONFIGURATION"


class InfrastructureError(AutomationError):
    """An operation on a file or external service could not complete."""

    CODE: ClassVar[str] = "INFRASTRUCTURE"


class AuthenticationError(InfrastructureError):
    """Credentials are missing, invalid, or require explicit user authorization."""

    CODE: ClassVar[str] = "AUTHENTICATION"


class AuthorizationError(InfrastructureError):
    """The authenticated identity lacks permission for the requested operation."""

    CODE: ClassVar[str] = "AUTHORIZATION"


class PersistenceError(InfrastructureError):
    """A local file could not be read, saved, or backed up."""

    CODE: ClassVar[str] = "PERSISTENCE"


class ExternalServiceError(InfrastructureError):
    """An external service failed without sufficient evidence of a transient cause."""

    CODE: ClassVar[str] = "SERVICE_EXTERNAL"


class TransientError(ExternalServiceError):
    """A temporary external failure may disappear after a safe retry."""

    CODE: ClassVar[str] = "TRANSIENT"


class OperationTimeoutError(TransientError):
    """An operation exceeded its timeout and its outcome may be uncertain."""

    CODE: ClassVar[str] = "TIME_WAIT"


class RegistrationInterruptedError(AutomationError):
    """A post-backup failure carries state for manual recovery review."""

    CODE: ClassVar[str] = "REGISTRATION_INTERRUPTED"

    def __init__(
        self,
        *,
        backup: Path | str,
        confirmed_sheets: tuple[str, ...],
        current_sheet: str | None,
    ) -> None:
        """Keep recovery state without exposing it in the public error message."""
        super().__init__(
            "Registration interrupted. Review the source and backup before continuing."
        )
        self.backup = backup
        self.confirmed_sheets = confirmed_sheets
        self.current_sheet = current_sheet
