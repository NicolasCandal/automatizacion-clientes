"""Nombres anteriores de los modelos y errores actuales, sin tipos duplicados."""

from automatizacion_clientes.core.errores import ConcurrenciaError as ErrorConcurrencia
from automatizacion_clientes.core.errores import EstructuraError as ErrorEstructura
from automatizacion_clientes.core.modelos import FilaCliente as FilaCliente
from automatizacion_clientes.core.modelos import HojaInfo as HojaInfo

__all__ = ["ErrorConcurrencia", "ErrorEstructura", "FilaCliente", "HojaInfo"]
