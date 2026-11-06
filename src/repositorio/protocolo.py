"""Nombre anterior de la fachada que delega en adaptadores de producción."""

from automatizacion_clientes.infra.repositorio_compatibilidad import (
    RepositorioCompatible as RepositorioClientes,
)

__all__ = ["RepositorioClientes"]
