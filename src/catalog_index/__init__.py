"""Legacy catalog facades backed by the main package's domain rules."""

from .normalization import extract_holders, generate_name_keys, normalize, sorted_name_key
from .reader import build_catalog

__all__ = [
    "build_catalog",
    "extract_holders",
    "generate_name_keys",
    "normalize",
    "sorted_name_key",
]
