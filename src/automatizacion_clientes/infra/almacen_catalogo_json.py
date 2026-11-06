"""Persistencia versionada del catálogo, sin consultar ni modificar su origen."""

import logging
from datetime import datetime
from pathlib import Path
from typing import Final

from automatizacion_clientes.core.errores import EstructuraError, PersistenciaError, ValidacionError
from automatizacion_clientes.core.modelos import (
    AparicionCliente,
    CatalogoClientes,
    HojaInfo,
    PersonaCliente,
    UbicacionCliente,
)
from automatizacion_clientes.infra.almacenamiento_json import ValorJson, guardar_json, leer_json

LOGGER = logging.getLogger(__name__)
VERSION_ESQUEMA_CATALOGO: Final[int] = 1


class AlmacenCatalogoJson:
    """Implementa ``AlmacenCatalogo`` mediante un documento JSON versionado.

    Cada lectura valida el documento completo y devuelve modelos inmutables.
    Conserva identidades, nombres, orden y todas las apariciones, incluidas varias
    filas de una misma hoja y ubicaciones compartidas entre cotitulares.

    No interpreta la revisión ni comprueba su vigencia contra la planilla. El
    servicio decide cuándo reconstruir la caché. El esquema anterior, que no
    conserva la última fila de clientes, requiere reconstrucción desde el origen.
    """

    def __init__(self, ruta: Path) -> None:
        """Recibe la ruta configurada sin acceder al disco ni crear carpetas.

        Args:
            ruta: Caché local, normalmente ``Config.catalogo_path``.
        """
        self._ruta = ruta

    def leer(self) -> CatalogoClientes | None:
        """Carga un catálogo válido o devuelve ``None`` únicamente si no existe.

        No repara, transforma ni sobrescribe archivos durante la lectura.

        Raises:
            EstructuraError: JSON inválido, versión no admitida, campos ausentes
                o desconocidos, o tipos incorrectos.
            ValidacionError: Identidades, referencias, límites o fechas inválidos.
            PersistenciaError: El archivo no se puede leer por otra causa.
        """
        try:
            documento = leer_json(self._ruta)
        except PersistenciaError as causa:
            if isinstance(causa.__cause__, FileNotFoundError):
                return None
            raise
        catalogo = _decodificar_catalogo(documento)
        LOGGER.info(
            "Catálogo validado: %s personas y %s hojas.",
            len(catalogo.personas),
            len(catalogo.hojas),
        )
        return catalogo

    def guardar(self, catalogo: CatalogoClientes) -> None:
        """Guarda el catálogo completo mediante reemplazo atómico.

        Puede sustituir una caché obsoleta o corrupta cuando el servicio haya
        obtenido un catálogo válido del origen. No exige una lectura previa.
        No bloquea escritores concurrentes; debe usarse un único escritor.

        Args:
            catalogo: Instantánea validada del origen, sin regenerar sus IDs.

        Raises:
            ValidacionError: Algún texto no se puede representar como JSON UTF-8.
            PersistenciaError: Falló la escritura o sustitución del documento.
        """
        hojas: list[ValorJson] = [
            {
                "nombre": hoja.nombre,
                "sheet_id": hoja.sheet_id,
                "col_nombre": hoja.col_nombre,
                "primera_col_semana": hoja.primera_col_semana,
                "ultima_col_semana": hoja.ultima_col_semana,
                "ultima_fila_con_cliente": hoja.ultima_fila_con_cliente,
            }
            for hoja in catalogo.hojas
        ]
        personas: list[ValorJson] = [_codificar_persona(persona) for persona in catalogo.personas]
        guardar_json(
            self._ruta,
            {
                "version_esquema": VERSION_ESQUEMA_CATALOGO,
                "identificador_origen": catalogo.identificador_origen,
                "revision": catalogo.revision,
                "generado_en": catalogo.generado_en.isoformat(),
                "hojas": hojas,
                "personas": personas,
            },
        )
        LOGGER.info(
            "Catálogo guardado: %s personas y %s hojas.",
            len(catalogo.personas),
            len(catalogo.hojas),
        )


def _decodificar_catalogo(documento: ValorJson) -> CatalogoClientes:
    if not isinstance(documento, dict) or "version_esquema" not in documento:
        raise EstructuraError(
            "El catálogo no tiene versión de esquema. Reconstrúyalo desde el origen."
        )
    version = documento["version_esquema"]
    if type(version) is not int or version != VERSION_ESQUEMA_CATALOGO:
        raise EstructuraError("La versión del esquema del catálogo no está admitida.")
    entrada = _leer_objeto(
        documento,
        {"version_esquema", "identificador_origen", "revision", "generado_en", "hojas", "personas"},
    )
    try:
        generado_en = datetime.fromisoformat(_leer_texto(entrada["generado_en"]))
    except ValueError as causa:
        raise ValidacionError(
            "La fecha de generación del catálogo no es un instante ISO válido."
        ) from causa
    return CatalogoClientes(
        identificador_origen=_leer_texto(entrada["identificador_origen"]),
        revision=_leer_texto(entrada["revision"]),
        generado_en=generado_en,
        hojas=tuple(_decodificar_hoja(hoja) for hoja in _leer_lista(entrada["hojas"])),
        personas=tuple(
            _decodificar_persona(persona) for persona in _leer_lista(entrada["personas"])
        ),
    )


def _decodificar_hoja(documento: ValorJson) -> HojaInfo:
    entrada = _leer_objeto(
        documento,
        {
            "nombre",
            "sheet_id",
            "col_nombre",
            "primera_col_semana",
            "ultima_col_semana",
            "ultima_fila_con_cliente",
        },
    )
    return HojaInfo(
        nombre=_leer_texto(entrada["nombre"]),
        sheet_id=_leer_entero(entrada["sheet_id"]),
        col_nombre=_leer_entero(entrada["col_nombre"]),
        primera_col_semana=_leer_columna_opcional(entrada["primera_col_semana"]),
        ultima_col_semana=_leer_columna_opcional(entrada["ultima_col_semana"]),
        ultima_fila_con_cliente=_leer_entero(entrada["ultima_fila_con_cliente"]),
    )


def _decodificar_persona(documento: ValorJson) -> PersonaCliente:
    entrada = _leer_objeto(
        documento, {"persona_id", "clave_orden", "nombre_canonico", "titulares", "apariciones"}
    )
    return PersonaCliente(
        persona_id=_leer_texto(entrada["persona_id"]),
        clave_orden=_leer_texto(entrada["clave_orden"]),
        nombre_canonico=_leer_texto(entrada["nombre_canonico"]),
        titulares=tuple(_leer_texto(titular) for titular in _leer_lista(entrada["titulares"])),
        apariciones=tuple(
            _decodificar_aparicion(aparicion) for aparicion in _leer_lista(entrada["apariciones"])
        ),
    )


def _decodificar_aparicion(documento: ValorJson) -> AparicionCliente:
    entrada = _leer_objeto(documento, {"hoja", "fila", "nombre_crudo"})
    return AparicionCliente(
        ubicacion=UbicacionCliente(
            hoja=_leer_texto(entrada["hoja"]), fila=_leer_entero(entrada["fila"])
        ),
        nombre_crudo=_leer_texto(entrada["nombre_crudo"]),
    )


def _codificar_persona(persona: PersonaCliente) -> dict[str, ValorJson]:
    titulares: list[ValorJson] = list(persona.titulares)
    apariciones: list[ValorJson] = [
        {
            "hoja": aparicion.ubicacion.hoja,
            "fila": aparicion.ubicacion.fila,
            "nombre_crudo": aparicion.nombre_crudo,
        }
        for aparicion in persona.apariciones
    ]
    return {
        "persona_id": persona.persona_id,
        "clave_orden": persona.clave_orden,
        "nombre_canonico": persona.nombre_canonico,
        "titulares": titulares,
        "apariciones": apariciones,
    }


def _leer_objeto(valor: ValorJson, campos: set[str]) -> dict[str, ValorJson]:
    if not isinstance(valor, dict) or valor.keys() != campos:
        raise EstructuraError("El catálogo contiene objetos con campos ausentes o desconocidos.")
    return valor


def _leer_lista(valor: ValorJson) -> list[ValorJson]:
    if not isinstance(valor, list):
        raise EstructuraError("Un campo de lista del catálogo tiene un tipo incorrecto.")
    return valor


def _leer_texto(valor: ValorJson) -> str:
    if not isinstance(valor, str):
        raise EstructuraError("Un campo de texto del catálogo tiene un tipo incorrecto.")
    return valor


def _leer_entero(valor: ValorJson) -> int:
    if type(valor) is not int:
        raise EstructuraError("Un índice del catálogo debe ser un entero, sin coerción de tipos.")
    return valor


def _leer_columna_opcional(valor: ValorJson) -> int | None:
    return None if valor is None else _leer_entero(valor)
