"""Credenciales OAuth y de servicio sin login implícito ni secretos en logs."""

import json
import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Final, Protocol, cast

from google.auth.credentials import Credentials
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials as CredencialesUsuario
from google.oauth2.service_account import Credentials as CredencialesServicio
from google_auth_oauthlib.flow import InstalledAppFlow
from requests import PreparedRequest, Response, Session
from requests.adapters import HTTPAdapter
from requests.exceptions import ConnectionError, ConnectTimeout, RequestException, SSLError, Timeout
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_random_exponential

from automatizacion_clientes.config import Config
from automatizacion_clientes.core.errores import (
    AutenticacionError,
    AutomatizacionError,
    AutorizacionError,
    ConcurrenciaError,
    ConfiguracionError,
    EstructuraError,
    PersistenciaError,
    ServicioExternoError,
    TiempoEsperaError,
    TransitorioError,
)
from automatizacion_clientes.infra.almacenamiento_json import ValorJson, guardar_json, leer_json

LOGGER = logging.getLogger(__name__)
URI_TOKEN: Final[str] = "https://oauth2.googleapis.com/token"
URI_AUTORIZACION: Final[frozenset[str]] = frozenset(
    {
        "https://accounts.google.com/o/oauth2/auth",
        "https://accounts.google.com/o/oauth2/v2/auth",
    }
)
type Objeto = dict[str, ValorJson]


class _EstadoCredencial(Protocol):
    @property
    def valid(self) -> bool: ...
    @property
    def scopes(self) -> Sequence[str] | None: ...
    @property
    def granted_scopes(self) -> Sequence[str] | None: ...
    def refresh(self, request: Request) -> None: ...
    def to_json(self) -> str: ...


class _FlujoOAuth(Protocol):
    oauth2session: Session

    def run_local_server(
        self,
        *,
        host: str,
        bind_addr: str,
        port: int,
        authorization_prompt_message: str,
        success_message: str,
        open_browser: bool,
        timeout_seconds: int,
    ) -> Credentials: ...


class _FabricaFlujo(Protocol):
    def __call__(
        self,
        client_config: Mapping[str, object],
        scopes: Sequence[str],
        *,
        autogenerate_code_verifier: bool,
    ) -> _FlujoOAuth: ...


class _ConexionInicialError(TiempoEsperaError):
    """La conexión no llegó a establecerse; el POST aún no fue enviado."""


class _FabricaServicio(Protocol):
    def __call__(
        self,
        info: Mapping[str, object],
        *,
        scopes: Sequence[str],
    ) -> Credentials: ...


class _AdaptadorToken(HTTPAdapter):
    def __init__(self, configuracion: Config, *, intercambio: bool) -> None:
        super().__init__(max_retries=0)
        self._config = configuracion
        self._intercambio = intercambio

    def send(
        self,
        request: PreparedRequest,
        stream: bool = False,
        timeout: float | tuple[float, float] | tuple[float, None] | None = None,
        verify: bool | str = True,
        cert: bytes | str | tuple[bytes | str, bytes | str] | None = None,
        proxies: Mapping[str, str] | None = None,
    ) -> Response:
        if request.url != URI_TOKEN or request.method != "POST":
            raise AutenticacionError("La autenticación requiere el endpoint de tokens de Google.")
        for intento in Retrying(
            retry=retry_if_exception(self._reintentar),
            stop=stop_after_attempt(self._config.reintentos_maximos),
            wait=wait_random_exponential(
                multiplier=self._config.reintento_espera_minima_segundos,
                min=self._config.reintento_espera_minima_segundos,
                max=self._config.reintento_espera_maxima_segundos,
            ),
            reraise=True,
        ):
            with intento:
                try:
                    respuesta = super().send(
                        request,
                        stream=stream,
                        timeout=self._config.timeout_red_segundos,
                        verify=verify,
                        cert=cert,
                        proxies=proxies,
                    )
                except SSLError as causa:
                    raise ServicioExternoError(
                        "No se pudo verificar TLS al autenticar Google."
                    ) from causa
                except ConnectTimeout as causa:
                    raise _ConexionInicialError(
                        "No se pudo conectar al servidor de autenticación."
                    ) from causa
                except Timeout as causa:
                    raise TiempoEsperaError(
                        "La autenticación excedió el tiempo permitido."
                    ) from causa
                except ConnectionError as causa:
                    raise TransitorioError(
                        "La conexión de autenticación falló temporalmente."
                    ) from causa
                except RequestException as causa:
                    raise ServicioExternoError(
                        "No se pudo completar la autenticación de Google."
                    ) from causa
                if 200 <= respuesta.status_code < 300:
                    return respuesta
                respuesta.close()
                if respuesta.status_code == 408:
                    raise TiempoEsperaError("Google agotó el tiempo de autenticación.")
                if respuesta.status_code == 429 or (
                    500 <= respuesta.status_code < 600 and respuesta.status_code not in {501, 505}
                ):
                    raise TransitorioError(
                        "El servidor de autenticación está temporalmente ocupado."
                    )
                if respuesta.status_code == 403:
                    raise AutorizacionError("Google rechazó los permisos de autenticación.")
                raise AutenticacionError("Google rechazó la autenticación. Revise la autorización.")
        raise ServicioExternoError("No se pudo terminar la autenticación de Google.")

    def _reintentar(self, causa: BaseException) -> bool:
        permitido = (
            isinstance(causa, _ConexionInicialError)
            if self._intercambio
            else isinstance(causa, TransitorioError)
        )
        if permitido:
            LOGGER.warning("Fallo temporal de autenticación; se evaluará otro intento.")
        return permitido


@contextmanager
def _silenciar_sdk() -> Iterator[None]:
    # Los SDK pueden registrar códigos OAuth y tokens al configurar DEBUG externo.
    prefijos = (
        "oauthlib",
        "requests_oauthlib",
        "google_auth_oauthlib",
        "google.auth",
        "google.oauth2",
    )
    anteriores = [
        (logger, logger.disabled)
        for nombre, logger in tuple(logging.Logger.manager.loggerDict.items())
        if isinstance(logger, logging.Logger) and nombre.startswith(prefijos)
    ]
    for logger, _ in anteriores:
        logger.disabled = True
    try:
        yield
    finally:
        for logger, deshabilitado in anteriores:
            logger.disabled = deshabilitado


@dataclass(frozen=True, slots=True)
class _TokenLeido:
    documento: Objeto | None = field(repr=False)


class ProveedorCredencialesGoogle:
    """Carga credenciales y realiza autorización inicial solo mediante una llamada explícita.

    No carga archivos ni hace red al construirse. La carga normal puede renovar
    tokens pero no los guarda salvo ``persistir=True``. Nunca borra un token
    revocado ni abre un navegador como recuperación automática. Un único proceso
    debe administrar cada archivo de token; no es un gestor concurrente.
    """

    def __init__(self, configuracion: Config) -> None:
        """Recibe configuración inmutable y valida destinos sin acceder a secretos."""
        self._config = configuracion
        protegidos = (
            configuracion.credentials_path,
            configuracion.excel_path,
            configuracion.contactos_path,
            configuracion.whatsapp_manifiesto_path,
            configuracion.catalogo_path,
            configuracion.aliases_path,
            configuracion.lista_negra_path,
            configuracion.resolucion_path,
        )
        if any(
            configuracion.token_path.is_relative_to(ruta)
            or ruta.is_relative_to(configuracion.token_path)
            for ruta in protegidos
        ):
            raise ConfiguracionError(
                "La ruta del token debe ser independiente de los demás archivos."
            )

    def cargar(self, *, renovar: bool = True, persistir: bool = False) -> Credentials:
        """Carga OAuth o cuenta de servicio y, si hace falta, renueva con timeout.

        Args:
            renovar: Permite obtener un token nuevo si las credenciales no son válidas.
            persistir: Guarda un token OAuth renovado; no guarda tokens de servicio.

        Returns:
            Credenciales válidas con los alcances requeridos por la configuración.

        Raises:
            AutenticacionError: Falta el token, su formato no es válido o requiere consentimiento.
            AutorizacionError: No se pueden acreditar los alcances solicitados.
            PersistenciaError: Falló una operación de archivo.
            ConcurrenciaError: Otro proceso cambió el token antes de guardarlo.
            InfraestructuraError: Falló la renovación después de sus intentos permitidos.
        """
        inicial = _TokenLeido(None)
        if self._config.google_tipo_autenticacion == "cuenta_servicio":
            credenciales = self._cargar_servicio()
        else:
            inicial = self._leer_token(opcional=True)
            if inicial.documento is None:
                raise AutenticacionError(
                    "Falta un token OAuth. Autorice Google de forma explícita."
                )
            self._validar_token(inicial.documento)
            fabricar = cast(
                Callable[[Mapping[str, object]], Credentials],
                CredencialesUsuario.from_authorized_user_info,
            )
            try:
                credenciales = fabricar(inicial.documento)
            except (ValueError, TypeError, GoogleAuthError) as causa:
                raise AutenticacionError("El token OAuth no tiene un formato válido.") from causa
        self._validar_alcances(credenciales)
        estado = cast(_EstadoCredencial, credenciales)
        renovada = not estado.valid
        if renovada:
            if not renovar:
                raise AutenticacionError("Las credenciales requieren renovación explícita.")
            with Session() as sesion:
                sesion.mount("https://", _AdaptadorToken(self._config, intercambio=False))
                with _silenciar_sdk():
                    try:
                        estado.refresh(Request(session=sesion))
                    except AutomatizacionError:
                        raise
                    except (
                        GoogleAuthError,
                        ValueError,
                        TypeError,
                        AttributeError,
                        KeyError,
                        OverflowError,
                    ) as causa:
                        raise AutenticacionError(
                            "No se pudo renovar la autorización de Google."
                        ) from causa
            if not estado.valid:
                raise AutenticacionError("La renovación no produjo credenciales válidas.")
            _texto(credenciales.token)
            self._validar_alcances(credenciales)
            if persistir and self._config.google_tipo_autenticacion == "oauth":
                self._guardar_token(credenciales, inicial)
        LOGGER.info("Credenciales de Google disponibles: renovadas=%s.", renovada)
        return credenciales

    def autorizar(self, *, persistir: bool = True) -> Credentials:
        """Autoriza OAuth de escritorio con navegador y callback IPv4 local.

        Solicita PKCE S256 y un puerto efímero en 127.0.0.1. No imprime la URL
        de autorización ni códigos. La espera del usuario tiene límite propio.
        Un intercambio de código ambiguo no se repite: solo se reintentan fallos
        de conexión inicial que indican que el POST aún no fue enviado.

        Args:
            persistir: Guarda el token obtenido tras verificar permisos y concurrencia.

        Returns:
            Credenciales del usuario que completó el consentimiento.

        Raises:
            ConfiguracionError: Se seleccionó un perfil distinto de OAuth.
            AutenticacionError: Cliente inválido, consentimiento fallido o espera agotada.
            AutomatizacionError: Falló la red o el guardado; no se borra el token anterior.
        """
        if self._config.google_tipo_autenticacion != "oauth":
            raise ConfiguracionError("La autorización interactiva requiere el perfil OAuth.")
        inicial = self._leer_token(opcional=True)
        documento = _objeto(leer_json(self._config.credentials_path))
        instalado = _objeto(documento.get("installed"))
        if (
            not isinstance(instalado.get("auth_uri"), str)
            or instalado.get("auth_uri") not in URI_AUTORIZACION
            or instalado.get("token_uri") != URI_TOKEN
        ):
            raise AutenticacionError("Configure un cliente OAuth de escritorio de Google válido.")
        for campo in ("client_id", "client_secret"):
            _texto(instalado.get(campo))
        fabricar = cast(_FabricaFlujo, InstalledAppFlow.from_client_config)
        flujo: _FlujoOAuth | None = None
        with _silenciar_sdk():
            try:
                flujo = fabricar(
                    documento, self._config.google_alcances, autogenerate_code_verifier=True
                )
                flujo.oauth2session.mount(
                    "https://", _AdaptadorToken(self._config, intercambio=True)
                )
                LOGGER.info("Autorización de Google iniciada en el navegador.")
                credenciales = flujo.run_local_server(
                    host="127.0.0.1",
                    bind_addr="127.0.0.1",
                    port=0,
                    authorization_prompt_message="",
                    success_message="Autorización completada. Puede cerrar esta ventana.",
                    open_browser=True,
                    timeout_seconds=self._config.google_timeout_autorizacion_segundos,
                )
            except AutomatizacionError:
                raise
            except Exception as causa:
                raise AutenticacionError(
                    "No se completó la autorización OAuth de Google."
                ) from causa
            finally:
                if flujo is not None:
                    try:
                        flujo.oauth2session.close()
                    except OSError:
                        LOGGER.warning("No se pudo cerrar la sesión OAuth de Google.")
        if not cast(_EstadoCredencial, credenciales).valid:
            raise AutenticacionError("La autorización no produjo credenciales válidas.")
        _texto(credenciales.token)
        self._validar_alcances(credenciales)
        if persistir:
            self._guardar_token(credenciales, inicial)
        LOGGER.info("Autorización de Google completada: token guardado=%s.", persistir)
        return credenciales

    def _cargar_servicio(self) -> Credentials:
        documento = _objeto(leer_json(self._config.credentials_path))
        if documento.get("type") != "service_account" or documento.get("token_uri") != URI_TOKEN:
            raise AutenticacionError("Configure un archivo de cuenta de servicio de Google válido.")
        for campo in ("client_email", "private_key"):
            _texto(documento.get(campo))
        fabricar = cast(_FabricaServicio, CredencialesServicio.from_service_account_info)
        try:
            return fabricar(documento, scopes=self._config.google_alcances)
        except (GoogleAuthError, ValueError, TypeError) as causa:
            raise AutenticacionError("La clave de cuenta de servicio no es válida.") from causa

    def _leer_token(self, *, opcional: bool) -> _TokenLeido:
        try:
            return _TokenLeido(_objeto(leer_json(self._config.token_path)))
        except PersistenciaError as causa:
            if opcional and isinstance(causa.__cause__, FileNotFoundError):
                return _TokenLeido(None)
            raise

    def _validar_token(self, documento: Objeto) -> None:
        if documento.get("token") is not None:
            _texto(documento["token"])
        if documento.get("type", "authorized_user") != "authorized_user":
            raise AutenticacionError(
                "El archivo de token debe contener credenciales OAuth de usuario."
            )
        if documento.get("token_uri", URI_TOKEN) != URI_TOKEN:
            raise AutenticacionError("El token no utiliza el endpoint admitido de Google.")
        for campo in ("client_id", "client_secret", "refresh_token"):
            _texto(documento.get(campo))
        alcances: object = documento.get("scopes")
        if isinstance(alcances, str):
            alcances = alcances.split()
        if (
            not isinstance(alcances, list)
            or not alcances
            or any(not isinstance(valor, str) or not valor.strip() for valor in alcances)
        ):
            raise AutorizacionError("El token debe declarar sus alcances de autorización.")

    def _validar_alcances(self, credenciales: Credentials) -> None:
        estado = cast(_EstadoCredencial, credenciales)
        concedidos: object = getattr(credenciales, "granted_scopes", None)
        disponibles = concedidos if concedidos is not None else estado.scopes
        if not isinstance(disponibles, (list, tuple)) or any(
            not isinstance(valor, str) for valor in disponibles
        ):
            raise AutorizacionError("No se pudieron acreditar los alcances de Google.")
        if not set(self._config.google_alcances) <= set(disponibles):
            raise AutorizacionError("La autorización de Google no incluye los alcances requeridos.")

    def _guardar_token(self, credenciales: Credentials, anterior: _TokenLeido) -> None:
        try:
            serializar = cast(_EstadoCredencial, credenciales).to_json
            documento = _objeto(json.loads(serializar()))
        except (ValueError, TypeError) as causa:
            raise AutenticacionError("No se pudo serializar el token de Google.") from causa
        concedidos: object = getattr(credenciales, "granted_scopes", None)
        if concedidos is not None:
            documento["scopes"] = list(cast(Sequence[str], concedidos))
        self._validar_token(documento)
        if self._leer_token(opcional=True) != anterior:
            raise ConcurrenciaError("El token cambió durante la autenticación. Abra otra sesión.")
        guardar_json(self._config.token_path, documento)


def _objeto(valor: object) -> Objeto:
    if not isinstance(valor, dict):
        raise EstructuraError("El archivo de autenticación debe contener un objeto JSON.")
    return cast(Objeto, valor)


def _texto(valor: object) -> str:
    if not isinstance(valor, str) or not valor.strip():
        raise AutenticacionError("Falta un campo requerido en las credenciales de Google.")
    return valor
