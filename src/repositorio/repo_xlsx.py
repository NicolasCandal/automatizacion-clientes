"""Fachada XLSX histórica delegada a los adaptadores actuales."""

from pathlib import Path

from automatizacion_clientes.core.contratos import EscritorClientes
from automatizacion_clientes.infra.escritor_clientes_excel import EscritorClientesExcel
from automatizacion_clientes.infra.lector_clientes_excel import LectorClientesExcel
from automatizacion_clientes.infra.reloj_sistema import RelojSistema
from automatizacion_clientes.infra.repositorio_compatibilidad import RepositorioCompatible


class RepoXlsx(RepositorioCompatible):
    """Conserva consultas históricas; las escrituras exigen revisión explícita."""

    def __init__(self, ruta: str | Path, *, directorio_respaldos: Path | None = None) -> None:
        """Construye adaptadores sin abrir el libro."""
        self.ruta = Path(ruta)
        self._escritor = EscritorClientesExcel(
            self.ruta,
            directorio_respaldos=directorio_respaldos or self.ruta.parent / "respaldos",
            reloj=RelojSistema(),
        )
        super().__init__(LectorClientesExcel(self.ruta))

    def obtener_escritor(self) -> EscritorClientes:
        """Devuelve el mismo escritor XLSX durante la sesión."""
        self.comprobar_abierto()
        return self._escritor
