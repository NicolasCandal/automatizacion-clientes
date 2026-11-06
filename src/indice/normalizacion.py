"""Compatibilidad de la API anterior con las funciones puras de ``core``.

Las reglas residen en ``automatizacion_clientes.core.normalizacion``. Esta capa
conserva los diccionarios y listas para consumidores de la API histórica.
"""

from automatizacion_clientes.core.normalizacion import clave_orden as clave_orden
from automatizacion_clientes.core.normalizacion import (
    extraer_titulares as _extraer_titulares,
)
from automatizacion_clientes.core.normalizacion import generar_claves as _generar_claves
from automatizacion_clientes.core.normalizacion import normalizar as normalizar
from automatizacion_clientes.core.normalizacion import quitar_tildes as quitar_tildes


def generar_claves(nombre_crudo: str) -> dict[str, str]:
    """Devuelve el formato anterior de claves sin duplicar la normalización."""
    claves = _generar_claves(nombre_crudo)
    return {
        "crudo": claves.crudo,
        "normalizado": claves.normalizado,
        "clave_orden": claves.clave_orden,
    }


def extraer_titulares(nombre_crudo: str) -> list[str]:
    """Adapta la tupla del dominio a la lista esperada por los módulos anteriores."""
    return list(_extraer_titulares(nombre_crudo))
