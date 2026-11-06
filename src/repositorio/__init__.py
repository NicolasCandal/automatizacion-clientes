"""Importaciones históricas de las fachadas actuales de repositorio."""

from .protocolo import RepositorioClientes
from .repo_sheets import RepoSheets
from .repo_xlsx import RepoXlsx
from .tipos import ErrorConcurrencia, ErrorEstructura, FilaCliente, HojaInfo

__all__ = [
    "ErrorConcurrencia",
    "ErrorEstructura",
    "FilaCliente",
    "HojaInfo",
    "RepoSheets",
    "RepoXlsx",
    "RepositorioClientes",
]
