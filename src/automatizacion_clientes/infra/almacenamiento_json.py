"""Lectura estricta de JSON y reemplazo atómico de documentos locales.

Este módulo valida el formato JSON, no el esquema de los modelos de negocio.
Los adaptadores de catálogos, aliases e informes realizan esa segunda validación.
"""

import json
import logging
from math import isfinite
from os import fsync
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import cast

from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from automatizacion_clientes.core.errores import (
    EstructuraError,
    PersistenciaError,
    ValidacionError,
)

type ValorJson = bool | int | float | str | list[ValorJson] | dict[str, ValorJson] | None

LOGGER = logging.getLogger(__name__)


def leer_json(ruta: Path) -> ValorJson:
    """Lee un documento UTF-8 y comprueba que contiene JSON válido.

    Acepta una marca BOM inicial para interoperar con archivos de Windows.
    Conserva los valores ``null``; un archivo ausente es un error de lectura.

    Args:
        ruta: Archivo que se debe leer.

    Returns:
        Valor JSON validado, todavía sin convertir a un modelo de negocio.

    Raises:
        EstructuraError: Contenido inválido, claves duplicadas, números no finitos
            o anidamiento excesivo.
        PersistenciaError: No se pudo leer el archivo, incluido si no existe.
    """
    try:
        contenido = ruta.read_text(encoding="utf-8-sig")
    except UnicodeError as causa:
        raise EstructuraError("El documento JSON no está codificado en UTF-8.") from causa
    except OSError as causa:
        raise PersistenciaError("No se pudo leer el documento JSON.") from causa

    try:
        documento: object = json.loads(contenido, object_pairs_hook=_objeto_sin_duplicados)
        _validar_valor(documento)
    except (ValueError, RecursionError) as causa:
        raise EstructuraError("El documento no contiene JSON válido.") from causa

    LOGGER.debug("Documento JSON leído y validado.")
    return cast(ValorJson, documento)


def guardar_json(ruta: Path, documento: ValorJson) -> None:
    """Guarda JSON mediante un temporal y reemplaza el destino al terminar.

    Serializa antes de tocar el disco. Crea los directorios que falten y escribe
    UTF-8 con claves ordenadas y salto de línea final. Sincroniza y cierra el
    temporal antes del reemplazo; un fallo previo conserva el destino anterior.
    El reemplazo reintenta únicamente los bloqueos de Windows 32 y 33, con
    cuatro intentos y esperas exponenciales de 0,05 a 0,2 segundos.

    El reemplazo no evita que dos escritores se sobrescriban ni garantiza
    durabilidad ante un corte de energía: no se sincroniza el directorio. Un
    cierre abrupto del proceso puede dejar un temporal para su limpieza posterior.
    No se modifica el contenido del destino a través de enlaces simbólicos.

    Args:
        ruta: Archivo de destino; el temporal se crea en la misma carpeta.
        documento: Valor compuesto exclusivamente por tipos JSON.

    Raises:
        ValidacionError: Tipos no admitidos, referencias cíclicas, números no
            finitos, texto no codificable en UTF-8 o anidamiento excesivo.
        PersistenciaError: Falló la creación, escritura o sustitución del archivo.
    """
    try:
        _validar_valor(documento)
        contenido = json.dumps(
            documento, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
        )
        contenido_utf8 = (contenido + "\n").encode("utf-8")
    except (ValueError, RecursionError) as causa:
        raise ValidacionError("El documento no se puede representar como JSON UTF-8.") from causa

    ruta_temporal: Path | None = None
    try:
        ruta.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(
            mode="wb", dir=ruta.parent, prefix=".json-", suffix=".tmp", delete=False
        ) as archivo_temporal:
            ruta_temporal = Path(archivo_temporal.name)
            archivo_temporal.write(contenido_utf8)
            archivo_temporal.flush()
            fsync(archivo_temporal.fileno())
        _reemplazar_temporal(ruta_temporal, ruta)
        ruta_temporal = None
    except OSError as causa:
        raise PersistenciaError("No se pudo guardar el documento JSON.") from causa
    finally:
        if ruta_temporal is not None:
            _eliminar_temporal(ruta_temporal)

    LOGGER.debug("Documento JSON guardado mediante reemplazo atómico.")


def _objeto_sin_duplicados(pares: list[tuple[str, ValorJson]]) -> dict[str, ValorJson]:
    resultado: dict[str, ValorJson] = {}
    for clave, valor in pares:
        if clave in resultado:
            raise ValueError("El objeto JSON contiene claves duplicadas.")
        resultado[clave] = valor
    return resultado


def _validar_valor(valor: object) -> None:
    if valor is None or isinstance(valor, (str, bool, int)):
        return
    if isinstance(valor, float):
        if not isfinite(valor):
            raise ValueError("JSON requiere números finitos.")
        return
    if isinstance(valor, list):
        for elemento in cast(list[object], valor):
            _validar_valor(elemento)
        return
    if isinstance(valor, dict):
        for clave, elemento in cast(dict[object, object], valor).items():
            if not isinstance(clave, str):
                raise ValueError("Las claves JSON deben ser cadenas.")
            _validar_valor(elemento)
        return
    raise ValueError("El documento contiene un tipo no admitido por JSON.")


def _eliminar_temporal(ruta: Path) -> None:
    try:
        ruta.unlink(missing_ok=True)
    except OSError:
        LOGGER.warning("No se pudo eliminar un archivo temporal JSON tras un fallo de guardado.")


def _reemplazar_temporal(ruta_temporal: Path, ruta_destino: Path) -> None:
    for intento in Retrying(
        retry=retry_if_exception(_es_bloqueo_temporal),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=0.05, min=0.05, max=0.2),
        before_sleep=_avisar_reintento,
        reraise=True,
    ):
        with intento:
            ruta_temporal.replace(ruta_destino)


def _es_bloqueo_temporal(causa: BaseException) -> bool:
    return isinstance(causa, OSError) and getattr(causa, "winerror", None) in {32, 33}


def _avisar_reintento(estado: RetryCallState) -> None:
    LOGGER.warning(
        "Reemplazo JSON bloqueado temporalmente; se reintentará tras el intento %s.",
        estado.attempt_number,
    )
