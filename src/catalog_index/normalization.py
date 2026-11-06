"""Legacy normalization API backed by the pure core functions."""

from client_automation.core.normalization import (
    extract_holders as _extract_holders,
)
from client_automation.core.normalization import generate_name_keys as _generate_keys
from client_automation.core.normalization import normalize as normalize
from client_automation.core.normalization import sorted_name_key as sorted_name_key
from client_automation.core.normalization import strip_accents as strip_accents


def generate_name_keys(raw_name: str) -> dict[str, str]:
    """Return legacy name keys without duplicating normalization rules."""
    keys = _generate_keys(raw_name)
    return {
        "raw": keys.raw,
        "normalized": keys.normalized,
        "sorted_name_key": keys.sorted_name_key,
    }


def extract_holders(raw_name: str) -> list[str]:
    """Adapt domain holder tuples to the legacy list interface."""
    return list(_extract_holders(raw_name))
