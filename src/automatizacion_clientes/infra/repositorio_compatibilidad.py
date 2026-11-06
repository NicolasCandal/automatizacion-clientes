"""Fachada de listas y conjuntos que delega en los adaptadores actuales."""

import logging
from pathlib import Path
from types import TracebackType
from typing import Self

from automatizacion_clientes.core.contratos import EscritorClientes
from automatizacion_clientes.core.errores import ConfiguracionError, InfraestructuraError
from automatizacion_clientes.core.modelos import FilaCliente, HojaInfo
from automatizacion_clientes.infra.lector_clientes_excel import LectorClientesExcel
from automatizacion_clientes.infra.lector_clientes_sheets import LectorClientesSheets

LOGGER = logging.getLogger(__name__)


class RepositorioCompatible:
    """Adapta únicamente colecciones; conserva las precondiciones de escritura."""

    def __init__(self, lector: LectorClientesExcel | LectorClientesSheets) -> None:
        """Recibe un lector ya construido y administra su ciclo de vida."""
        self.lector = lector
        self._cerrado = False

    def listar_hojas(self) -> list[HojaInfo]:
        """Adapta la estructura validada a una lista."""
        return list(self.lector.listar_hojas())

    def leer_clientes(self, hoja: str) -> list[FilaCliente]:
        """Conserva todas las filas y los nombres originales."""
        return list(self.lector.leer_clientes(hoja))

    def ultima_semana(self, hoja: str) -> tuple[int, str]:
        """Consulta el encabezado del lector actual."""
        return self.lector.ultima_semana(hoja)

    def leer_marcas(self, hoja: str, col: int) -> set[int]:
        """Adapta las marcas a un conjunto independiente."""
        return set(self.lector.leer_marcas(hoja, col))

    def revision(self) -> str:
        """Obtiene una revisión actualizada que incluye datos y formato."""
        return self.lector.revision()

    def obtener_escritor(self) -> EscritorClientes:
        """Debe proporcionar el escritor actual del mismo origen."""
        raise NotImplementedError

    def comprobar_abierto(self) -> None:
        """Impide crear recursos después de cerrar la fachada."""
        if self._cerrado:
            raise ConfiguracionError("El repositorio está cerrado. Cree otra sesión.")

    def crear_respaldo(self) -> Path | str:
        """Delega el respaldo en el escritor actual."""
        return self.obtener_escritor().crear_respaldo()

    def aplicar_marcas(
        self,
        hoja: str,
        col: int,
        encabezado: str,
        filas: set[int],
        *,
        revision_esperada: str | None = None,
    ) -> None:
        """Exige revisión explícita; nunca la obtiene silenciosamente para escribir."""
        if revision_esperada is None:
            raise ConfiguracionError("La API anterior de escritura requiere revision_esperada.")
        self.obtener_escritor().aplicar_marcas(
            hoja, col, encabezado, frozenset(filas), revision_esperada=revision_esperada
        )

    def cerrar(self) -> None:
        """Libera el lector y lo invalida."""
        if not self._cerrado:
            self._cerrado = True
            self.lector.cerrar()

    def __enter__(self) -> Self:
        """Permite liberar recursos mediante un contexto."""
        self.comprobar_abierto()
        return self

    def __exit__(
        self,
        tipo: type[BaseException] | None,
        error: BaseException | None,
        traza: TracebackType | None,
    ) -> None:
        """No oculta un error del bloque con otro de cierre."""
        try:
            self.cerrar()
        except InfraestructuraError:
            if error is None:
                raise
            LOGGER.warning("No se pudo cerrar un adaptador de compatibilidad tras un fallo.")
