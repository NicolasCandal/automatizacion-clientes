"""Logging temporal de consola, con fechas UTC y sin crear archivos."""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from time import gmtime


@contextmanager
def logging_consola() -> Iterator[logging.Logger]:
    """Instala un manejador para el comando y restaura el estado al finalizar.

    Solo configura el logger del paquete. No reemplaza manejadores ajenos ni
    modifica el logger raíz; el nivel inicial INFO permite diagnosticar errores
    de argumentos o configuración antes de conocer ``AC_LOG_LEVEL``.
    """
    paquete = logging.getLogger("automatizacion_clientes")
    nivel_anterior = paquete.level
    propagacion_anterior = paquete.propagate
    manejador = logging.StreamHandler()
    formato = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    formato.converter = gmtime
    manejador.setFormatter(formato)
    paquete.addHandler(manejador)
    paquete.setLevel(logging.INFO)
    paquete.propagate = False
    try:
        yield paquete
    finally:
        paquete.removeHandler(manejador)
        manejador.close()
        paquete.setLevel(nivel_anterior)
        paquete.propagate = propagacion_anterior
