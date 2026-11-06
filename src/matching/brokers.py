"""Compatibilidad del reconocimiento de brokers con los módulos anteriores.

La lógica reside en ``core.brokers``; esta capa conserva las listas de salida
anteriores y la tabla mutable que ya exportaba el paquete ``matching``.
"""

from automatizacion_clientes.core.brokers import (
    TABLA_BROKERS_PREDETERMINADA,
)
from automatizacion_clientes.core.brokers import (
    extraer_brokers_y_nombre as _extraer_brokers_y_nombre,
)

TABLA_BROKERS_DEFAULT: dict[str, str] = dict(TABLA_BROKERS_PREDETERMINADA)


def extraer_brokers_y_nombre(
    nombre_crudo: str, tabla_brokers: dict[str, str] | None = None
) -> tuple[str, list[str], list[str]]:
    """Adapta el resultado del dominio al formato consumido por el motor actual."""
    tabla = TABLA_BROKERS_DEFAULT if tabla_brokers is None else tabla_brokers
    resultado = _extraer_brokers_y_nombre(nombre_crudo, tabla)
    return resultado.nombre_sin_parentesis, list(resultado.hojas), list(resultado.ruido)
