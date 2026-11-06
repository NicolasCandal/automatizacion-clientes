"""Nombre anterior del adaptador actual de exclusiones."""

from pathlib import Path

from automatizacion_clientes.infra.lista_exclusion_json import ListaExclusionJson


class ListaNegra(ListaExclusionJson):
    """Mantiene la construcción con str o Path y el guardado inmediato de agregar."""

    def __init__(self, ruta: str | Path) -> None:
        """Carga el formato histórico con validación estricta."""
        super().__init__(Path(ruta))

    def guardar(self) -> None:
        """No hay cambios pendientes: agregar ya persiste de forma atómica."""
