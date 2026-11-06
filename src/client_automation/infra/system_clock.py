"""System-clock access isolated from domain rules and application services."""

from datetime import UTC, datetime


class SystemClock:
    """Implement Clock with timezone-aware UTC instants."""

    def now(self) -> datetime:
        """Return the current UTC instant, preserving microseconds."""
        return datetime.now(UTC)
