"""Reconocimiento puro de brokers en los paréntesis de un nombre de contacto."""

from __future__ import annotations

import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from .modelos import NombreConBrokers
from .normalizacion import normalizar

TABLA_BROKERS_PREDETERMINADA: Final[Mapping[str, str]] = MappingProxyType(
    {
        "iol": "IOL",
        "invertironline": "IOL",
        "bull": "Bull",
        "bm": "Bull",
        "bull market": "Bull",
        "balanz": "Balanz",
        "ppi": "PPI",
        "portfolio personal": "PPI",
        "inviu": "Inviu",
    }
)

_PATRON_PARENTESIS: Final[re.Pattern[str]] = re.compile(r"\(([^)]*)\)")
_PATRON_SEPARADOR_BROKERS: Final[re.Pattern[str]] = re.compile(
    r"\s*/\s*|\s+y\s+|\s*-\s*|\s*,\s*", re.IGNORECASE
)
_PATRON_ESPACIOS: Final[re.Pattern[str]] = re.compile(r"\s+")


def extraer_brokers_y_nombre(
    nombre_crudo: str,
    tabla_brokers: Mapping[str, str] | None = None,
) -> NombreConBrokers:
    """Separa el nombre, las hojas reconocidas y el ruido entre paréntesis.

    Conserva las reglas anteriores: procesa todos los grupos reconocidos por
    el patrón de paréntesis y separa su contenido por barra, `` y ``, guion o
    coma. Normaliza cada token para consultar la tabla, conserva el primer
    orden de aparición de las hojas y no deduplica los tokens desconocidos.

    El texto fuera de los paréntesis conserva su capitalización y puntuación;
    solo se recortan y colapsan sus espacios. No se buscan brokers fuera de
    los paréntesis ni se eligen personas o filas de destino.

    Args:
        nombre_crudo: Nombre original del contacto.
        tabla_brokers: Alias previamente normalizados y sus hojas canónicas.
            ``None`` usa la tabla predeterminada; una tabla vacía desactiva el
            reconocimiento. La función no modifica la tabla recibida.

    Returns:
        Modelo inmutable con el nombre sin paréntesis, las hojas sin duplicados
        y los tokens de ruido con su formato original recortado.
    """
    tabla = TABLA_BROKERS_PREDETERMINADA if tabla_brokers is None else tabla_brokers
    contenidos_parentesis = _PATRON_PARENTESIS.findall(nombre_crudo)
    nombre_sin_parentesis = _PATRON_PARENTESIS.sub("", nombre_crudo).strip()
    nombre_sin_parentesis = _PATRON_ESPACIOS.sub(" ", nombre_sin_parentesis)

    hojas: list[str] = []
    hojas_vistas: set[str] = set()
    ruido: list[str] = []
    for contenido in contenidos_parentesis:
        for token in _PATRON_SEPARADOR_BROKERS.split(contenido):
            token_limpio = token.strip()
            if not token_limpio:
                continue
            hoja = tabla.get(normalizar(token_limpio))
            if hoja is None:
                ruido.append(token_limpio)
            elif hoja not in hojas_vistas:
                hojas_vistas.add(hoja)
                hojas.append(hoja)

    return NombreConBrokers(
        nombre_sin_parentesis=nombre_sin_parentesis,
        hojas=tuple(hojas),
        ruido=tuple(ruido),
    )
