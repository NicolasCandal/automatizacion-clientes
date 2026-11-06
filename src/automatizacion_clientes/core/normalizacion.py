"""Normalización y separación de titulares con las reglas del proyecto actual.

Funciones puras: no leen configuración ni archivos y no deciden coincidencias.
Se conserva la heurística existente para no cambiar las identidades aprendidas
durante la migración. Los originales se mantienen en ``ClavesNombre.crudo``.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final

from .modelos import ClavesNombre

_PATRON_PUNTUACION: Final[re.Pattern[str]] = re.compile(r"[^\w\s]", re.UNICODE)
_PATRON_ESPACIOS: Final[re.Pattern[str]] = re.compile(r"\s+")
_PATRON_Y_O: Final[re.Pattern[str]] = re.compile(r"\s+y/o\s+", re.IGNORECASE)
_PATRON_Y_SEPARADOR: Final[re.Pattern[str]] = re.compile(r"\s+y\s+", re.IGNORECASE)
_PATRON_PARENTESIS: Final[re.Pattern[str]] = re.compile(r"\(([^)]*)\)")
_PATRON_PREFIJO_RELACION: Final[re.Pattern[str]] = re.compile(
    r"^(esposa de|esposo de|esposa|esposo|hijo de|hija de|hijo|hija)\s+",
    re.IGNORECASE,
)


def quitar_tildes(texto: str) -> str:
    """Descompone el texto con NFKD y elimina sus marcas combinantes.

    Conserva las mayúsculas y los espacios recibidos. Esta regla también
    transforma la eñe y las ligaduras compatibles, como en la versión anterior.

    Args:
        texto: Texto original, incluida una posible cadena vacía.

    Returns:
        Texto sin marcas combinantes; la entrada no se modifica.
    """
    texto_descompuesto = unicodedata.normalize("NFKD", texto)
    return "".join(
        caracter for caracter in texto_descompuesto if not unicodedata.combining(caracter)
    )


def normalizar(texto: str) -> str:
    r"""Aplica minúsculas, NFKD, sustitución de puntuación y colapso de espacios.

    Se conserva el orden de operaciones y la definición Unicode de ``\w`` de
    la implementación anterior, que incluye dígitos y guiones bajos. La función
    no elimina brokers ni detecta cotitulares: esas son operaciones separadas.

    Args:
        texto: Nombre o texto que se desea comparar.

    Returns:
        Clave normalizada, o una cadena vacía si no hay caracteres significativos.
    """
    texto_sin_tildes = quitar_tildes(texto.strip().lower())
    texto_sin_puntuacion = _PATRON_PUNTUACION.sub(" ", texto_sin_tildes)
    return _PATRON_ESPACIOS.sub(" ", texto_sin_puntuacion).strip()


def clave_orden(texto: str) -> str:
    """Construye una clave independiente del orden de nombres y apellidos.

    Args:
        texto: Nombre original o previamente normalizado.

    Returns:
        Palabras normalizadas y ordenadas alfabéticamente, con repeticiones.
    """
    return " ".join(sorted(normalizar(texto).split()))


def generar_claves(nombre_crudo: str) -> ClavesNombre:
    """Genera las claves de comparación conservando exactamente la entrada.

    Args:
        nombre_crudo: Nombre con su formato original.

    Returns:
        Modelo inmutable con el original y ambas claves derivadas. Una entrada
        vacía produce claves vacías; el servicio decide si debe descartarla.
    """
    nombre_normalizado = normalizar(nombre_crudo)
    return ClavesNombre(
        crudo=nombre_crudo,
        normalizado=nombre_normalizado,
        clave_orden=clave_orden(nombre_normalizado),
    )


def extraer_titulares(nombre_crudo: str) -> tuple[str, ...]:
    """Separa los titulares de una celda usando la heurística histórica.

    Prioriza `` Y/O `` sobre ``/`` y usa `` Y `` solo si genera exactamente dos
    partes. Procesa el primer paréntesis balanceado: un prefijo familiar
    reconocido incorpora un titular adicional y el resto se considera ruido.
    Los demás paréntesis se conservan, igual que en la implementación anterior.

    No se deduplican ni normalizan los titulares. La celda original debe seguir
    asociada a su fila; la separación no crea nuevas filas en la planilla.

    Args:
        nombre_crudo: Texto original de la celda o nombre del contacto.

    Returns:
        Titulares en orden, con espacios externos recortados y sin partes vacías.
    """
    texto = nombre_crudo.strip()
    titulares_parentesis: list[str] = []
    coincidencia = _PATRON_PARENTESIS.search(texto)
    if coincidencia is not None:
        contenido = coincidencia.group(1).strip()
        texto = (texto[: coincidencia.start()] + texto[coincidencia.end() :]).strip()
        contenido_sin_relacion = _PATRON_PREFIJO_RELACION.sub("", contenido).strip()
        if contenido_sin_relacion and contenido_sin_relacion != contenido:
            titulares_parentesis.append(contenido_sin_relacion)

    if _PATRON_Y_O.search(texto):
        partes = _PATRON_Y_O.split(texto)
    elif "/" in texto:
        partes = texto.split("/")
    else:
        partes_con_y = _PATRON_Y_SEPARADOR.split(texto)
        partes = partes_con_y if len(partes_con_y) == 2 else [texto]

    titulares = [parte.strip() for parte in partes if parte.strip()]
    titulares.extend(titulares_parentesis)
    return tuple(titulares)
