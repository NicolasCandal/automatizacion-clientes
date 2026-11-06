"""Legacy facades for the current matching engine and persistence."""

from .alias_store import AliasStore
from .brokers import DEFAULT_BROKER_TABLE, extract_brokers_and_name
from .engine import build_index, resolve_contact, resolve_week
from .exclusion_list import ExclusionList

__all__ = [
    "DEFAULT_BROKER_TABLE",
    "AliasStore",
    "ExclusionList",
    "build_index",
    "extract_brokers_and_name",
    "resolve_contact",
    "resolve_week",
]
