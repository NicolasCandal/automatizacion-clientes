"""Persistencia versionada y validada de los resultados del motor semanal."""

import logging
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Final

from automatizacion_clientes.core.errores import EstructuraError, ValidacionError
from automatizacion_clientes.core.modelos import (
    CandidatoCoincidencia,
    ContactoIgnorado,
    ContactoPendiente,
    ContactoResuelto,
    ContactoSemanal,
    MotivoIgnorado,
    MotivoPendiente,
    ResolucionSemanal,
    UbicacionCliente,
    ViaCoincidencia,
)
from automatizacion_clientes.infra.almacenamiento_json import ValorJson, guardar_json, leer_json

LOGGER = logging.getLogger(__name__)
VERSION_ESQUEMA_RESOLUCIONES: Final[int] = 1


class AlmacenResolucionesJson:
    """Implementa ``AlmacenResoluciones`` conservando el informe completo.

    Mantiene el orden, las repeticiones legítimas por cotitularidad y los avisos
    originales. Los avisos pueden contener texto sensible y nunca se registran
    en logs. No reconstruye informes antiguos que carecen de fechas y teléfonos.

    El informe no contiene una revisión del catálogo: los servicios deben
    revalidar sus destinos contra el origen antes de aplicar cualquier marca.
    Leer o guardar un informe no modifica la planilla ni aprende aliases.
    """

    def __init__(self, ruta: Path) -> None:
        """Recibe la ruta de salida sin acceder al disco ni crear directorios.

        Args:
            ruta: Informe local, normalmente ``Config.resolucion_path``.
        """
        self._ruta = ruta

    def leer(self) -> ResolucionSemanal:
        """Lee y valida todos los resultados antes de devolver el informe.

        Cada llamada vuelve a leer el archivo. La ausencia o corrupción no
        equivale a un informe vacío y no produce resultados parciales.

        Raises:
            EstructuraError: Versión, campos, tipos, estados o enumeraciones
                incompatibles con el esquema.
            ValidacionError: Fechas, identidades, puntuaciones o relaciones inválidas.
            PersistenciaError: El archivo no existe o no se puede leer.
        """
        documento = leer_json(self._ruta)
        if not isinstance(documento, dict) or "version_esquema" not in documento:
            raise EstructuraError(
                "El informe no tiene versión de esquema. Genérelo con el servicio nuevo."
            )
        version = documento["version_esquema"]
        if type(version) is not int or version != VERSION_ESQUEMA_RESOLUCIONES:
            raise EstructuraError("La versión del esquema del informe no está admitida.")
        entrada = _leer_objeto(
            documento, {"version_esquema", "resueltos", "pendientes", "ignorados", "avisos"}
        )
        resolucion = ResolucionSemanal(
            resueltos=tuple(
                _decodificar_resuelto(valor) for valor in _leer_lista(entrada["resueltos"])
            ),
            pendientes=tuple(
                _decodificar_pendiente(valor) for valor in _leer_lista(entrada["pendientes"])
            ),
            ignorados=tuple(
                _decodificar_ignorado(valor) for valor in _leer_lista(entrada["ignorados"])
            ),
            avisos=tuple(_leer_texto(valor) for valor in _leer_lista(entrada["avisos"])),
        )
        _registrar_resumen("leído", resolucion)
        return resolucion

    def guardar(self, resolucion: ResolucionSemanal) -> None:
        """Guarda todos los resultados mediante reemplazo atómico.

        No necesita lectura previa y requiere un único escritor. Un fallo antes
        del reemplazo conserva el informe anterior; puede reintentarse tras
        resolver su causa. Las colecciones se conservan en su orden original.

        Args:
            resolucion: Resultados tipados producidos o revisados por los servicios.

        Raises:
            ValidacionError: Algún texto no puede representarse como JSON UTF-8.
            PersistenciaError: Falló la escritura o sustitución del documento.
        """
        resueltos: list[ValorJson] = [_codificar_resuelto(valor) for valor in resolucion.resueltos]
        pendientes: list[ValorJson] = [
            _codificar_pendiente(valor) for valor in resolucion.pendientes
        ]
        ignorados: list[ValorJson] = [
            {
                "tipo": valor.tipo,
                "contacto": _codificar_contacto(valor.contacto),
                "motivo": valor.motivo.value,
            }
            for valor in resolucion.ignorados
        ]
        avisos: list[ValorJson] = list(resolucion.avisos)
        guardar_json(
            self._ruta,
            {
                "version_esquema": VERSION_ESQUEMA_RESOLUCIONES,
                "resueltos": resueltos,
                "pendientes": pendientes,
                "ignorados": ignorados,
                "avisos": avisos,
            },
        )
        _registrar_resumen("guardado", resolucion)


def _decodificar_resuelto(documento: ValorJson) -> ContactoResuelto:
    entrada = _leer_resultado(
        documento,
        {"tipo", "contacto", "persona_id", "ubicaciones", "puntuacion", "via"},
        "resuelto",
    )
    return ContactoResuelto(
        contacto=_decodificar_contacto(entrada["contacto"]),
        persona_id=_leer_texto(entrada["persona_id"]),
        ubicaciones=tuple(
            _decodificar_ubicacion(valor) for valor in _leer_lista(entrada["ubicaciones"])
        ),
        puntuacion=_leer_puntuacion(entrada["puntuacion"]),
        via=_leer_enum(entrada["via"], ViaCoincidencia),
    )


def _decodificar_pendiente(documento: ValorJson) -> ContactoPendiente:
    entrada = _leer_resultado(documento, {"tipo", "contacto", "motivo", "candidatos"}, "pendiente")
    return ContactoPendiente(
        contacto=_decodificar_contacto(entrada["contacto"]),
        motivo=_leer_enum(entrada["motivo"], MotivoPendiente),
        candidatos=tuple(
            _decodificar_candidato(valor) for valor in _leer_lista(entrada["candidatos"])
        ),
    )


def _decodificar_ignorado(documento: ValorJson) -> ContactoIgnorado:
    entrada = _leer_resultado(documento, {"tipo", "contacto", "motivo"}, "ignorado")
    return ContactoIgnorado(
        contacto=_decodificar_contacto(entrada["contacto"]),
        motivo=_leer_enum(entrada["motivo"], MotivoIgnorado),
    )


def _decodificar_contacto(documento: ValorJson) -> ContactoSemanal:
    entrada = _leer_objeto(documento, {"nombre_crudo", "ultima_fecha", "telefono", "es_grupo"})
    telefono = entrada["telefono"]
    es_grupo = entrada["es_grupo"]
    if not (telefono is None or isinstance(telefono, str)) or not isinstance(es_grupo, bool):
        raise EstructuraError(
            "El teléfono o el indicador de grupo del informe tiene un tipo inválido."
        )
    texto_fecha = _leer_texto(entrada["ultima_fecha"])
    try:
        ultima_fecha = date.fromisoformat(texto_fecha)
    except ValueError as causa:
        raise ValidacionError("La fecha del contacto del informe no es válida.") from causa
    if ultima_fecha.isoformat() != texto_fecha:
        raise ValidacionError("La fecha del contacto del informe debe usar YYYY-MM-DD.")
    return ContactoSemanal(
        nombre_crudo=_leer_texto(entrada["nombre_crudo"]),
        ultima_fecha=ultima_fecha,
        telefono=telefono,
        es_grupo=es_grupo,
    )


def _decodificar_ubicacion(documento: ValorJson) -> UbicacionCliente:
    entrada = _leer_objeto(documento, {"hoja", "fila"})
    fila = entrada["fila"]
    if type(fila) is not int:
        raise EstructuraError("La fila de destino del informe debe ser un entero.")
    return UbicacionCliente(hoja=_leer_texto(entrada["hoja"]), fila=fila)


def _decodificar_candidato(documento: ValorJson) -> CandidatoCoincidencia:
    entrada = _leer_objeto(
        documento, {"persona_id", "nombre_canonico", "hojas", "puntuacion", "via"}
    )
    hojas = tuple(_leer_texto(valor) for valor in _leer_lista(entrada["hojas"]))
    if len(hojas) != len(set(hojas)):
        raise EstructuraError("Las hojas de un candidato del informe no pueden repetirse.")
    return CandidatoCoincidencia(
        persona_id=_leer_texto(entrada["persona_id"]),
        nombre_canonico=_leer_texto(entrada["nombre_canonico"]),
        hojas=frozenset(hojas),
        puntuacion=_leer_puntuacion(entrada["puntuacion"]),
        via=_leer_enum(entrada["via"], ViaCoincidencia),
    )


def _codificar_contacto(contacto: ContactoSemanal) -> dict[str, ValorJson]:
    return {
        "nombre_crudo": contacto.nombre_crudo,
        "ultima_fecha": contacto.ultima_fecha.isoformat(),
        "telefono": contacto.telefono,
        "es_grupo": contacto.es_grupo,
    }


def _codificar_resuelto(resultado: ContactoResuelto) -> dict[str, ValorJson]:
    ubicaciones: list[ValorJson] = [
        {"hoja": ubicacion.hoja, "fila": ubicacion.fila} for ubicacion in resultado.ubicaciones
    ]
    return {
        "tipo": resultado.tipo,
        "contacto": _codificar_contacto(resultado.contacto),
        "persona_id": resultado.persona_id,
        "ubicaciones": ubicaciones,
        "puntuacion": resultado.puntuacion,
        "via": resultado.via.value,
    }


def _codificar_pendiente(resultado: ContactoPendiente) -> dict[str, ValorJson]:
    candidatos: list[ValorJson] = []
    for candidato in resultado.candidatos:
        hojas: list[ValorJson] = list(sorted(candidato.hojas))
        candidatos.append(
            {
                "persona_id": candidato.persona_id,
                "nombre_canonico": candidato.nombre_canonico,
                "hojas": hojas,
                "puntuacion": candidato.puntuacion,
                "via": candidato.via.value,
            }
        )
    return {
        "tipo": resultado.tipo,
        "contacto": _codificar_contacto(resultado.contacto),
        "motivo": resultado.motivo.value,
        "candidatos": candidatos,
    }


def _leer_objeto(valor: ValorJson, campos: set[str]) -> dict[str, ValorJson]:
    if not isinstance(valor, dict) or valor.keys() != campos:
        raise EstructuraError("El informe contiene objetos con campos ausentes o desconocidos.")
    return valor


def _leer_resultado(valor: ValorJson, campos: set[str], tipo: str) -> dict[str, ValorJson]:
    entrada = _leer_objeto(valor, campos)
    if entrada["tipo"] != tipo:
        raise EstructuraError("El tipo de resultado no corresponde a su colección en el informe.")
    return entrada


def _leer_lista(valor: ValorJson) -> list[ValorJson]:
    if not isinstance(valor, list):
        raise EstructuraError("Un campo de lista del informe tiene un tipo incorrecto.")
    return valor


def _leer_texto(valor: ValorJson) -> str:
    if not isinstance(valor, str):
        raise EstructuraError("Un campo de texto del informe tiene un tipo incorrecto.")
    return valor


def _leer_puntuacion(valor: ValorJson) -> float:
    if not isinstance(valor, (int, float)) or isinstance(valor, bool):
        raise EstructuraError(
            "Las puntuaciones del informe deben ser números, sin coerción de tipos."
        )
    try:
        return float(valor)
    except OverflowError as causa:
        raise ValidacionError("La puntuación del informe excede la escala permitida.") from causa


def _leer_enum[TipoEnum: StrEnum](valor: ValorJson, tipo_enum: type[TipoEnum]) -> TipoEnum:
    try:
        return tipo_enum(_leer_texto(valor))
    except ValueError as causa:
        raise EstructuraError("El informe contiene una vía o un motivo desconocido.") from causa


def _registrar_resumen(accion: str, resolucion: ResolucionSemanal) -> None:
    LOGGER.info(
        "Informe %s: %s resueltos, %s pendientes, %s ignorados y %s avisos.",
        accion,
        len(resolucion.resueltos),
        len(resolucion.pendientes),
        len(resolucion.ignorados),
        len(resolucion.avisos),
    )
