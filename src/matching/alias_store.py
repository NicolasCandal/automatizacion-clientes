"""Fachada de aliases históricos sobre la persistencia y conciliación actuales."""

from pathlib import Path

from automatizacion_clientes.core.errores import ValidacionError
from automatizacion_clientes.infra.almacen_aliases_json import AlmacenAliasesJson
from automatizacion_clientes.infra.formatos_compatibilidad import (
    AliasHistorico,
    DestinoHistorico,
    alias_a_historico,
    alias_desde_historico,
)


class AliasStore:
    """Conserva la interfaz de diccionarios sin duplicar estado o reglas de conflicto."""

    def __init__(self, ruta: str | Path) -> None:
        """Abre una sesión tipada; un archivo ausente no se crea."""
        self.ruta = Path(ruta)
        self.almacen = AlmacenAliasesJson(self.ruta)

    def buscar_por_nombre(self, nombre_crudo: str) -> AliasHistorico | None:
        """Devuelve una copia de la asociación por nombre exacto."""
        alias = self.almacen.buscar_por_nombre(nombre_crudo)
        return None if alias is None else alias_a_historico(alias)

    def buscar_por_telefono(self, telefono: str | None) -> tuple[str, AliasHistorico] | None:
        """Rechaza teléfonos compartidos que la interfaz singular no puede representar."""
        if not telefono:
            return None
        aliases = self.almacen.buscar_por_telefono(telefono)
        if len(aliases) > 1:
            raise ValidacionError(
                "La interfaz anterior no puede elegir entre aliases de un teléfono."
            )
        return (aliases[0].nombre_crudo, alias_a_historico(aliases[0])) if aliases else None

    def registrar(
        self, nombre_crudo: str, personas: list[DestinoHistorico], telefono: str | None = None
    ) -> str | None:
        """Delega la conciliación y mantiene los cambios en memoria hasta guardar."""
        return self.almacen.registrar(
            alias_desde_historico(nombre_crudo, {"personas": personas, "telefono": telefono})
        )

    def guardar(self) -> None:
        """Persiste mediante reemplazo atómico y comprobación de cambios externos."""
        self.almacen.guardar()

    def __len__(self) -> int:
        """Consulta la cantidad de asociaciones sin leer el archivo."""
        return len(self.almacen)
