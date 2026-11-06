"""Fachada Sheets sin cliente Drive ni implementación HTTP duplicada."""

from google.auth.credentials import Credentials

from automatizacion_clientes.config import Config
from automatizacion_clientes.core.contratos import EscritorClientes
from automatizacion_clientes.core.errores import ConfiguracionError
from automatizacion_clientes.infra.escritor_clientes_sheets import (
    EscritorClientesSheets,
    crear_escritor_sheets,
)
from automatizacion_clientes.infra.lector_clientes_sheets import crear_lector_sheets
from automatizacion_clientes.infra.reloj_sistema import RelojSistema
from automatizacion_clientes.infra.repositorio_compatibilidad import RepositorioCompatible


class RepoSheets(RepositorioCompatible):
    """Delega lectura, revisiones y respaldo local; requiere permisos explícitos al escribir."""

    def __init__(
        self, spreadsheet_id: str, credenciales: Credentials, *, configuracion: Config | None = None
    ) -> None:
        """Construye una sesión lectora sin consultar la red ni abrir credenciales."""
        self._config = configuracion or Config(
            repositorio="sheets", spreadsheet_id=spreadsheet_id, _env_file=None
        )
        if self._config.spreadsheet_id != spreadsheet_id:
            raise ConfiguracionError("La configuración y el documento solicitado deben coincidir.")
        self._credenciales = credenciales
        self._escritor: EscritorClientesSheets | None = None
        super().__init__(crear_lector_sheets(self._config, credenciales))

    def obtener_escritor(self) -> EscritorClientes:
        """Crea el escritor solo al solicitar una operación que lo necesite."""
        self.comprobar_abierto()
        if self._escritor is None:
            self._escritor = crear_escritor_sheets(
                self._config, self._credenciales, reloj=RelojSistema()
            )
        return self._escritor

    def cerrar(self) -> None:
        """Cierra sesiones lectoras y escritoras, incluso si alguna falla."""
        if self._cerrado:
            return
        try:
            if self._escritor is not None:
                self._escritor.cerrar()
        finally:
            super().cerrar()
