"""Legacy imports for the current repository facades."""

from .protocol import ClientRepository
from .repo_sheets import RepoSheets
from .repo_xlsx import RepoXlsx
from .types import ClientRow, ConcurrencyError, SheetInfo, StructureError

__all__ = [
    "ClientRepository",
    "ClientRow",
    "ConcurrencyError",
    "RepoSheets",
    "RepoXlsx",
    "SheetInfo",
    "StructureError",
]
