"""Diagnósticos de los comandos retirados sin procesar entradas antiguas."""

import logging
import sys
from collections.abc import Sequence

from automatizacion_clientes.infra.logging_consola import logging_consola

LOGGER = logging.getLogger(__name__)


def comando_retirado(argumentos: Sequence[str] | None = None) -> int:
    """Informa la migración sin cargar configuración, abrir archivos o mostrar argumentos."""
    opciones = tuple(sys.argv[1:] if argumentos is None else argumentos)
    with logging_consola():
        LOGGER.warning(
            "Este comando anterior fue retirado. Use automatizacion-clientes --help. "
            "No se ejecutó ninguna operación."
        )
    return 0 if opciones in {("--help",), ("-h",)} else 2
