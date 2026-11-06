"""Fachadas históricas del catálogo; las reglas pertenecen al paquete principal."""

from .lector import construir_catalogo
from .normalizacion import clave_orden, extraer_titulares, generar_claves, normalizar

__all__ = [
    "clave_orden",
    "construir_catalogo",
    "extraer_titulares",
    "generar_claves",
    "normalizar",
]
