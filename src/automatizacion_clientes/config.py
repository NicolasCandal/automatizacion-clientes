"""Configuración validada sin efectos secundarios sobre los datos de clientes."""

from __future__ import annotations

from pathlib import Path
from typing import Final, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .core.modelos import PoliticaCoincidencia

RUTA_ENV_PREDETERMINADA: Final[Path] = Path(".env")

TipoEntorno = Literal["desarrollo", "test", "produccion"]
TipoRepositorio = Literal["excel", "sheets"]
NivelLog = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Config(BaseSettings):
    """Valida las opciones de ejecución obtenidas de argumentos, entorno y .env.

    Los argumentos explícitos tienen prioridad sobre el entorno y el archivo
    .env. Todas las rutas se resuelven respecto de ``directorio_base``.
    La carga no crea directorios, abre credenciales ni contacta servicios.

    Attributes:
        directorio_base: Base común para resolver las rutas relativas.
        repositorio: Implementación de almacenamiento seleccionada.
        spreadsheet_id: Identificador necesario para Google Sheets.
        simulacion: Generar un plan sin modificar la planilla de clientes.
    """

    model_config = SettingsConfigDict(
        env_prefix="AC_",
        env_file=RUTA_ENV_PREDETERMINADA,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
        validate_default=True,
        str_strip_whitespace=True,
        hide_input_in_errors=True,
    )

    directorio_base: Path = Field(default_factory=Path.cwd)
    entorno: TipoEntorno = "desarrollo"
    log_level: NivelLog = "INFO"
    zona_horaria: str = "America/Argentina/Buenos_Aires"

    directorio_datos: Path = Path("datos")
    directorio_logs: Path = Path("logs")
    contactos_path: Path = Path("datos/entrada/semana.json")
    whatsapp_manifiesto_path: Path = Path("datos/entrada/whatsapp/manifiesto.json")
    excel_path: Path = Path("datos/entrada/clientes.xlsx")
    catalogo_path: Path = Path("datos/estado/catalogo.json")
    aliases_path: Path = Path("datos/estado/aliases.json")
    lista_negra_path: Path = Path("datos/estado/lista_negra.json")
    resolucion_path: Path = Path("datos/salida/resolucion.json")
    directorio_respaldos: Path = Path("datos/respaldos")

    repositorio: TipoRepositorio = "excel"
    spreadsheet_id: str | None = Field(default=None, repr=False)
    credentials_path: Path = Path("secretos/credentials.json")
    token_path: Path = Path("secretos/token.json")
    google_tipo_autenticacion: Literal["oauth", "cuenta_servicio"] = "oauth"
    google_alcances: tuple[str, ...] = ("https://www.googleapis.com/auth/spreadsheets.readonly",)
    google_timeout_autorizacion_segundos: int = Field(default=180, ge=30, le=600)

    reintentos_maximos: int = Field(default=4, ge=1, le=10)
    reintento_espera_minima_segundos: float = Field(default=1.0, gt=0, allow_inf_nan=False)
    reintento_espera_maxima_segundos: float = Field(default=30.0, gt=0, allow_inf_nan=False)
    timeout_red_segundos: float = Field(default=30.0, gt=0, allow_inf_nan=False)
    umbral_coincidencia_automatico: float = Field(default=92.0, ge=0, le=100, allow_inf_nan=False)
    umbral_coincidencia_revision: float = Field(default=80.0, ge=0, le=100, allow_inf_nan=False)
    limite_sugerencias_coincidencia: int = Field(default=3, ge=1)
    simulacion: bool = True

    @field_validator(
        "directorio_base",
        "directorio_datos",
        "directorio_logs",
        "contactos_path",
        "whatsapp_manifiesto_path",
        "excel_path",
        "catalogo_path",
        "aliases_path",
        "lista_negra_path",
        "resolucion_path",
        "directorio_respaldos",
        "credentials_path",
        "token_path",
        mode="before",
    )
    @classmethod
    def _rechazar_ruta_vacia(cls, valor: object) -> object:
        """Evita convertir una cadena vacía en el directorio de trabajo."""
        if isinstance(valor, str):
            if not valor.strip():
                raise ValueError("La ruta no puede estar vacía.")
            return valor.strip()
        return valor

    @field_validator("directorio_base")
    @classmethod
    def _resolver_directorio_base(cls, ruta: Path) -> Path:
        """Normaliza la base sin crearla ni exigir que exista."""
        return ruta.expanduser().resolve()

    @field_validator(
        "directorio_datos",
        "directorio_logs",
        "contactos_path",
        "whatsapp_manifiesto_path",
        "excel_path",
        "catalogo_path",
        "aliases_path",
        "lista_negra_path",
        "resolucion_path",
        "directorio_respaldos",
        "credentials_path",
        "token_path",
    )
    @classmethod
    def _resolver_ruta(cls, ruta: Path, informacion: ValidationInfo) -> Path:
        """Ancla las rutas relativas a la base validada."""
        directorio_base = informacion.data.get("directorio_base")
        if not isinstance(directorio_base, Path):
            raise ValueError("Se necesita un directorio_base válido para resolver las rutas.")
        ruta_expandida = ruta.expanduser()
        if not ruta_expandida.is_absolute():
            ruta_expandida = directorio_base / ruta_expandida
        return ruta_expandida.resolve()

    @field_validator("spreadsheet_id", mode="before")
    @classmethod
    def _normalizar_identificador(cls, valor: object) -> object:
        """Interpreta identificadores vacíos como una opción sin configurar."""
        if isinstance(valor, str):
            return valor.strip() or None
        return valor

    @field_validator("zona_horaria")
    @classmethod
    def _validar_zona_horaria(cls, nombre: str) -> str:
        """Comprueba que la zona exista en la base IANA."""
        try:
            ZoneInfo(nombre)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("La zona horaria debe ser un identificador IANA válido.") from error
        return nombre

    @model_validator(mode="after")
    def _validar_opciones_relacionadas(self) -> Self:
        """Rechaza opciones inconsistentes antes de ejecutar la automatización."""
        if self.repositorio == "sheets" and self.spreadsheet_id is None:
            raise ValueError("AC_SPREADSHEET_ID es obligatorio al usar Google Sheets.")
        if self.reintento_espera_minima_segundos > self.reintento_espera_maxima_segundos:
            raise ValueError("La espera mínima de reintentos no puede superar la máxima.")
        if self.credentials_path == self.token_path:
            raise ValueError("Las credenciales y el token deben almacenarse en archivos distintos.")
        self.crear_politica_coincidencia()
        return self

    @field_validator("google_alcances")
    @classmethod
    def _validar_alcances_google(cls, alcances: tuple[str, ...]) -> tuple[str, ...]:
        """Exige alcances explícitos de Sheets/Drive, sin duplicados ni espacios."""
        permitidos = {
            "https://www.googleapis.com/auth/spreadsheets.readonly",
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive.readonly",
            "https://www.googleapis.com/auth/drive.file",
            "https://www.googleapis.com/auth/drive",
        }
        if not alcances or len(set(alcances)) != len(alcances) or not set(alcances) <= permitidos:
            raise ValueError("Configure alcances únicos y admitidos de Google Sheets o Drive.")
        return alcances

    def crear_politica_coincidencia(self) -> PoliticaCoincidencia:
        """Convierte las opciones del entorno en una política pura e inmutable."""
        return PoliticaCoincidencia(
            umbral_automatico=self.umbral_coincidencia_automatico,
            umbral_revision=self.umbral_coincidencia_revision,
            limite_sugerencias=self.limite_sugerencias_coincidencia,
        )


def cargar_config(
    ruta_env: Path | None = RUTA_ENV_PREDETERMINADA,
    *,
    directorio_base: Path | None = None,
) -> Config:
    """Carga una configuración nueva; no mantiene cachés ni modifica el entorno.

    Args:
        ruta_env: Archivo de configuración, relativo a la base si corresponde.
            Con ``None`` se desactiva la lectura de archivos .env.
        directorio_base: Base de las rutas; por defecto, el directorio actual.
            Este argumento prevalece sobre ``AC_DIRECTORIO_BASE``.

    Returns:
        Configuración validada e inmutable con rutas absolutas.

    Raises:
        pydantic.ValidationError: Si alguna opción es inválida o inconsistente.
        OSError: Si no se puede resolver o leer una ruta de configuración.
    """
    if directorio_base is not None:
        base_explicita = directorio_base.expanduser().resolve()
        archivo_env = ruta_env.expanduser() if ruta_env is not None else None
        if archivo_env is not None and not archivo_env.is_absolute():
            archivo_env = base_explicita / archivo_env
        return Config(directorio_base=base_explicita, _env_file=archivo_env)
    return Config(_env_file=ruta_env.expanduser() if ruta_env is not None else None)
