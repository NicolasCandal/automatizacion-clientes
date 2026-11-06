"""Fachadas históricas sobre el motor y la persistencia actuales."""

from .alias_store import AliasStore
from .brokers import TABLA_BROKERS_DEFAULT, extraer_brokers_y_nombre
from .lista_negra import ListaNegra
from .motor import construir_indice, resolver_contacto, resolver_semana

__all__ = [
    "TABLA_BROKERS_DEFAULT",
    "AliasStore",
    "ListaNegra",
    "construir_indice",
    "extraer_brokers_y_nombre",
    "resolver_contacto",
    "resolver_semana",
]
