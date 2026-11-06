"""Legacy broker recognition interface backed by the domain implementation."""

from client_automation.core.brokers import (
    DEFAULT_BROKER_TABLE as DOMAIN_BROKER_TABLE,
)
from client_automation.core.brokers import (
    extract_brokers_and_name as _extract_brokers_and_name,
)

DEFAULT_BROKER_TABLE: dict[str, str] = dict(DOMAIN_BROKER_TABLE)


def extract_brokers_and_name(
    raw_name: str, table_brokers: dict[str, str] | None = None
) -> tuple[str, list[str], list[str]]:
    """Adapt the domain result to the legacy matching interface."""
    table = DEFAULT_BROKER_TABLE if table_brokers is None else table_brokers
    result = _extract_brokers_and_name(raw_name, table)
    return result.name_without_parentheses, list(result.sheets), list(result.noise)
