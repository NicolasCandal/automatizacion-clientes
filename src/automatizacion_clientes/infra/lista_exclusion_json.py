"""Exclusiones persistidas como una lista JSON de nombres originales."""

import logging
from pathlib import Path

from automatizacion_clientes.core.errores import (
    ConcurrenciaError,
    EstructuraError,
    PersistenciaError,
    ValidacionError,
)
from automatizacion_clientes.core.normalizacion import normalizar
from automatizacion_clientes.infra.almacenamiento_json import ValorJson, guardar_json, leer_json

LOGGER = logging.getLogger(__name__)


class ListaExclusionJson:
    """Implementa ``ListaExclusion`` con guardado inmediato y atómico.

    Usa la normalización del dominio para consultar y evitar nuevas entradas
    equivalentes. Conserva los nombres originales, incluso los duplicados que
    pudiera contener un archivo histórico; no los elimina al cargar o guardar.

    Las consultas usan la instantánea cargada. Cada llamada a ``agregar`` valida
    que el archivo no cambió, incluso si el nombre ya está excluido. Esta
    comprobación no bloquea otros procesos: debe existir un único escritor.
    """

    def __init__(self, ruta: Path) -> None:
        """Carga las exclusiones sin modificar el archivo ni crear carpetas.

        Args:
            ruta: Archivo local, normalmente obtenido de ``Config``.

        Raises:
            EstructuraError: El documento no es una lista JSON de cadenas.
            ValidacionError: Algún nombre carece de texto significativo.
            PersistenciaError: No se puede leer por una causa distinta de ausencia.
        """
        self._ruta = ruta
        self._persistidos = _cargar_exclusiones(ruta)
        self._normalizados = frozenset(normalizar(nombre) for nombre in self._persistidos or ())

    def contiene(self, nombre_crudo: str) -> bool:
        """Consulta la instantánea por nombre normalizado sin acceder al disco.

        Las consultas sin caracteres significativos devuelven ``False``. No
        reordena palabras, elimina brokers ni aplica comparación aproximada.
        """
        return normalizar(nombre_crudo) in self._normalizados

    def __len__(self) -> int:
        """Devuelve la cantidad de nombres persistidos, incluidos duplicados históricos."""
        return len(self._persistidos or ())

    def agregar(self, nombre_crudo: str) -> None:
        """Añade una exclusión y actualiza la sesión solo después de guardarla.

        Repetir un nombre equivalente no escribe en disco. Un fallo previo al
        reemplazo conserva tanto el archivo anterior como la instantánea local;
        permite reintentar explícitamente cuando se resuelva la causa.

        Args:
            nombre_crudo: Nombre original que se desea excluir.

        Raises:
            ValidacionError: El nombre nuevo o uno persistido no contiene texto
                significativo o no puede guardarse como JSON UTF-8.
            ConcurrenciaError: El archivo cambió desde la carga o el último guardado.
            EstructuraError: El archivo actual está corrupto o tiene otro esquema.
            PersistenciaError: No se pudo comprobar o guardar el documento.
        """
        nombre_normalizado = _validar_nombre(nombre_crudo)
        if _cargar_exclusiones(self._ruta) != self._persistidos:
            raise ConcurrenciaError(
                "La lista de exclusión cambió. Abra otra sesión antes de registrar exclusiones."
            )
        if nombre_normalizado in self._normalizados:
            return

        nombres_actualizados = tuple(sorted((*(self._persistidos or ()), nombre_crudo)))
        documento: list[ValorJson] = list(nombres_actualizados)
        guardar_json(self._ruta, documento)
        self._persistidos = nombres_actualizados
        self._normalizados = self._normalizados | {nombre_normalizado}
        LOGGER.info("Exclusión registrada y guardada.")


def _cargar_exclusiones(ruta: Path) -> tuple[str, ...] | None:
    try:
        documento = leer_json(ruta)
    except PersistenciaError as causa:
        if isinstance(causa.__cause__, FileNotFoundError):
            return None
        raise

    if not isinstance(documento, list):
        raise EstructuraError("La lista de exclusión debe ser una lista JSON de cadenas.")
    nombres: list[str] = []
    for nombre in documento:
        if not isinstance(nombre, str):
            raise EstructuraError("La lista de exclusión solo admite nombres de texto.")
        _validar_nombre(nombre)
        nombres.append(nombre)
    return tuple(nombres)


def _validar_nombre(nombre_crudo: str) -> str:
    nombre_normalizado = normalizar(nombre_crudo)
    if not nombre_normalizado:
        raise ValidacionError("El nombre de exclusión debe contener texto significativo.")
    return nombre_normalizado
