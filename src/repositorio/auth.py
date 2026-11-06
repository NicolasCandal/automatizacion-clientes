"""Compatibilidad de carga OAuth sin navegador ni eliminación automática de tokens."""

from pathlib import Path

from google.auth.credentials import Credentials

from automatizacion_clientes.config import Config
from automatizacion_clientes.core.errores import AutenticacionError as ErrorAutenticacion
from automatizacion_clientes.infra.autenticacion_google import ProveedorCredencialesGoogle


def obtener_credenciales(
    ruta_credentials: str | Path = Path("secretos/credentials.json"),
    ruta_token: str | Path = Path("secretos/token.json"),
    *,
    configuracion: Config | None = None,
) -> Credentials:
    """Carga mediante el proveedor actual; la autorización inicial es una operación separada.

    Sin Config explícita se conserva la carga del entorno AC_, pero no se lee
    .env implícitamente. No se solicitan alcances de Drive por defecto.
    """
    opciones = configuracion or Config(
        credentials_path=Path(ruta_credentials), token_path=Path(ruta_token), _env_file=None
    )
    return ProveedorCredencialesGoogle(opciones).cargar(persistir=False)


__all__ = ["ErrorAutenticacion", "obtener_credenciales"]
