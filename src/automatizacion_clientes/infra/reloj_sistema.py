"""Acceso al reloj del sistema aislado de las reglas y de los servicios."""

from datetime import UTC, datetime


class RelojSistema:
    """Implementa el contrato ``Reloj`` usando instantes conscientes de UTC."""

    def ahora(self) -> datetime:
        """Devuelve el instante actual en UTC, conservando microsegundos."""
        return datetime.now(UTC)
