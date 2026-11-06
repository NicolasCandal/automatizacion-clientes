"""Credenciales ficticias, renovación HTTP simulada y OAuth sin navegador real."""

import json
import logging
from collections.abc import Callable, Mapping
from copy import deepcopy
from os import environ
from pathlib import Path
from typing import cast

import pytest
from google.auth.credentials import Credentials
from google.auth.crypt import Signer
from google.oauth2.credentials import Credentials as CredencialesUsuario
from google.oauth2.service_account import Credentials as CredencialesServicio
from google_auth_oauthlib.flow import InstalledAppFlow
from pydantic import ValidationError
from pytest_mock import MockerFixture
from requests import Request as Solicitud
from requests import Response
from requests.adapters import HTTPAdapter
from requests.exceptions import ConnectionError, ConnectTimeout, SSLError, Timeout

from automatizacion_clientes.config import Config, cargar_config
from automatizacion_clientes.core.errores import (
    AutenticacionError,
    AutorizacionError,
    ConcurrenciaError,
    ConfiguracionError,
    EstructuraError,
    PersistenciaError,
    ServicioExternoError,
    TiempoEsperaError,
    TransitorioError,
)
from automatizacion_clientes.infra import autenticacion_google as modulo
from automatizacion_clientes.infra.almacenamiento_json import ValorJson, guardar_json, leer_json
from automatizacion_clientes.infra.autenticacion_google import ProveedorCredencialesGoogle
from automatizacion_clientes.infra.lector_clientes_sheets import crear_lector_sheets

ALCANCE = "https://www.googleapis.com/auth/spreadsheets.readonly"
type Objeto = dict[str, ValorJson]


@pytest.fixture(autouse=True)
def aislar_entorno(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture) -> None:
    for nombre in tuple(environ):
        if nombre.upper().startswith("AC_"):
            monkeypatch.delenv(nombre)
    monkeypatch.chdir(tmp_path)
    mocker.patch("requests.sessions.get_netrc_auth", return_value=None)


@pytest.fixture
def configuracion(tmp_path: Path) -> Config:
    return Config(
        directorio_base=tmp_path,
        _env_file=None,
        reintentos_maximos=3,
        reintento_espera_minima_segundos=0.001,
        reintento_espera_maxima_segundos=0.002,
    )


@pytest.fixture
def token() -> Objeto:
    return {
        "token": "acceso_ficticio",
        "refresh_token": "renovacion_ficticia",
        "client_id": "cliente_ficticio",
        "client_secret": "secreto_ficticio",
        "token_uri": modulo.URI_TOKEN,
        "scopes": [ALCANCE],
        "expiry": "2099-01-01T00:00:00Z",
    }


def credenciales(documento: Objeto) -> Credentials:
    fabricar = cast(
        Callable[[Mapping[str, object]], Credentials], CredencialesUsuario.from_authorized_user_info
    )
    return fabricar(documento)


def respuesta(estado: int = 200, **contenido: object) -> Response:
    resultado = Response()
    resultado.status_code = estado
    resultado.encoding = "utf-8"
    resultado._content = json.dumps(
        contenido
        or {
            "access_token": "acceso_nuevo_ficticio",
            "expires_in": 3600,
            "token_type": "Bearer",
            "scope": ALCANCE,
        }
    ).encode("utf-8")
    _ = resultado.content
    return resultado


def escribir_cliente(configuracion: Config) -> None:
    guardar_json(
        configuracion.credentials_path,
        {
            "installed": {
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": modulo.URI_TOKEN,
                "client_id": "cliente_ficticio",
                "client_secret": "secreto_ficticio",
                "redirect_uris": ["http://localhost"],
            }
        },
    )


def test_constructor_no_abre_archivos_ni_hace_red(
    configuracion: Config, mocker: MockerFixture
) -> None:
    lectura = mocker.patch.object(modulo, "leer_json")
    enviar = mocker.patch.object(HTTPAdapter, "send")
    ProveedorCredencialesGoogle(configuracion)
    lectura.assert_not_called()
    enviar.assert_not_called()
    assert not configuracion.token_path.parent.exists()


def test_token_valido_no_necesita_cliente_secreto_ni_renovacion(
    configuracion: Config, token: Objeto, mocker: MockerFixture
) -> None:
    guardar_json(configuracion.token_path, token)
    original = configuracion.token_path.read_bytes()
    enviar = mocker.patch.object(HTTPAdapter, "send")
    resultado = ProveedorCredencialesGoogle(configuracion).cargar(persistir=True)
    assert resultado.token == "acceso_ficticio"
    assert resultado.valid
    enviar.assert_not_called()
    assert configuracion.token_path.read_bytes() == original
    assert not configuracion.credentials_path.exists()


def test_token_ausente_no_abre_navegador_ni_crea_directorios(
    configuracion: Config, mocker: MockerFixture
) -> None:
    flujo = mocker.patch.object(InstalledAppFlow, "from_client_config")
    with pytest.raises(AutenticacionError):
        ProveedorCredencialesGoogle(configuracion).cargar()
    flujo.assert_not_called()
    assert not configuracion.token_path.parent.exists()


@pytest.mark.parametrize("persistir", [False, True])
def test_renovacion_guarda_solo_si_se_solicita(
    configuracion: Config, token: Objeto, mocker: MockerFixture, persistir: bool
) -> None:
    token["expiry"] = "2000-01-01T00:00:00Z"
    guardar_json(configuracion.token_path, token)
    original = configuracion.token_path.read_bytes()
    enviar = mocker.patch.object(HTTPAdapter, "send", return_value=respuesta())
    resultado = ProveedorCredencialesGoogle(configuracion).cargar(persistir=persistir)
    assert resultado.valid
    assert resultado.token == "acceso_nuevo_ficticio"
    enviar.assert_called_once()
    assert enviar.call_args.kwargs["timeout"] == configuracion.timeout_red_segundos
    assert (configuracion.token_path.read_bytes() != original) is persistir
    if persistir:
        assert cast(Objeto, leer_json(configuracion.token_path))["token"] == "acceso_nuevo_ficticio"


def test_alcances_guardados_como_cadena_no_causan_conflicto_falso(
    configuracion: Config, token: Objeto, mocker: MockerFixture
) -> None:
    token.update({"scopes": ALCANCE, "expiry": "2000-01-01T00:00:00Z"})
    guardar_json(configuracion.token_path, token)
    mocker.patch.object(HTTPAdapter, "send", return_value=respuesta())
    assert ProveedorCredencialesGoogle(configuracion).cargar(persistir=True).valid


def test_renovar_false_rechaza_token_vencido_sin_red(
    configuracion: Config, token: Objeto, mocker: MockerFixture
) -> None:
    token["expiry"] = "2000-01-01T00:00:00Z"
    guardar_json(configuracion.token_path, token)
    enviar = mocker.patch.object(HTTPAdapter, "send")
    with pytest.raises(AutenticacionError):
        ProveedorCredencialesGoogle(configuracion).cargar(renovar=False)
    enviar.assert_not_called()


@pytest.mark.parametrize("estado", [400, 401, 403])
def test_token_revocado_o_sin_permisos_no_se_borra_ni_reintenta(
    configuracion: Config, token: Objeto, mocker: MockerFixture, estado: int
) -> None:
    token["expiry"] = "2000-01-01T00:00:00Z"
    guardar_json(configuracion.token_path, token)
    original = configuracion.token_path.read_bytes()
    enviar = mocker.patch.object(
        HTTPAdapter, "send", return_value=respuesta(estado, error="invalid_grant")
    )
    with pytest.raises(AutorizacionError if estado == 403 else AutenticacionError):
        ProveedorCredencialesGoogle(configuracion).cargar(persistir=True)
    enviar.assert_called_once()
    assert configuracion.token_path.read_bytes() == original


@pytest.mark.parametrize("estado", [408, 429, 500, 502, 503, 504])
def test_renovacion_reintenta_fallos_transitorios(
    configuracion: Config, token: Objeto, mocker: MockerFixture, estado: int
) -> None:
    token["expiry"] = "2000-01-01T00:00:00Z"
    guardar_json(configuracion.token_path, token)
    enviar = mocker.patch.object(
        HTTPAdapter, "send", side_effect=[respuesta(estado, error="temporary"), respuesta()]
    )
    assert ProveedorCredencialesGoogle(configuracion).cargar().valid
    assert enviar.call_count == 2


def test_fallo_persistente_respeta_limite_sin_reintentos_ocultos(
    configuracion: Config, token: Objeto, mocker: MockerFixture
) -> None:
    token["expiry"] = "2000-01-01T00:00:00Z"
    guardar_json(configuracion.token_path, token)
    enviar = mocker.patch.object(HTTPAdapter, "send", side_effect=Timeout("secreto"))
    with pytest.raises(TiempoEsperaError):
        ProveedorCredencialesGoogle(configuracion).cargar()
    assert enviar.call_count == configuracion.reintentos_maximos


@pytest.mark.parametrize("causa", [SSLError("secreto"), ConnectionError("secreto")])
def test_tls_no_se_reintenta_y_conexion_si(
    configuracion: Config, token: Objeto, mocker: MockerFixture, causa: Exception
) -> None:
    token["expiry"] = "2000-01-01T00:00:00Z"
    guardar_json(configuracion.token_path, token)
    enviar = mocker.patch.object(HTTPAdapter, "send", side_effect=causa)
    with pytest.raises(ServicioExternoError):
        ProveedorCredencialesGoogle(configuracion).cargar()
    assert enviar.call_count == (
        1 if isinstance(causa, SSLError) else configuracion.reintentos_maximos
    )


def test_renovacion_parcial_de_permisos_no_sobrescribe_token(
    configuracion: Config, token: Objeto, mocker: MockerFixture
) -> None:
    token["expiry"] = "2000-01-01T00:00:00Z"
    guardar_json(configuracion.token_path, token)
    original = configuracion.token_path.read_bytes()
    mocker.patch.object(
        HTTPAdapter,
        "send",
        return_value=respuesta(access_token="nuevo", expires_in=3600, scope="otro_alcance"),
    )
    with pytest.raises(AutorizacionError):
        ProveedorCredencialesGoogle(configuracion).cargar(persistir=True)
    assert configuracion.token_path.read_bytes() == original


def test_cambio_de_token_durante_renovacion_no_se_sobrescribe(
    configuracion: Config, token: Objeto, mocker: MockerFixture
) -> None:
    token["expiry"] = "2000-01-01T00:00:00Z"
    guardar_json(configuracion.token_path, token)
    otro = deepcopy(token)
    otro["token"] = "externo_ficticio"

    def responder(*argumentos: object, **opciones: object) -> Response:
        guardar_json(configuracion.token_path, otro)
        return respuesta()

    mocker.patch.object(HTTPAdapter, "send", side_effect=responder)
    with pytest.raises(ConcurrenciaError):
        ProveedorCredencialesGoogle(configuracion).cargar(persistir=True)
    assert leer_json(configuracion.token_path) == otro


def test_token_malformado_y_sin_permisos_no_disparan_login(
    configuracion: Config, token: Objeto, mocker: MockerFixture
) -> None:
    flujo = mocker.patch.object(InstalledAppFlow, "from_client_config")
    token["scopes"] = ["https://www.googleapis.com/auth/drive"]
    guardar_json(configuracion.token_path, token)
    with pytest.raises(AutorizacionError):
        ProveedorCredencialesGoogle(configuracion).cargar()
    configuracion.token_path.write_bytes(b"{invalido")
    with pytest.raises(EstructuraError):
        ProveedorCredencialesGoogle(configuracion).cargar()
    flujo.assert_not_called()


@pytest.mark.parametrize(
    ("campo", "valor"),
    [
        ("token_uri", "https://ejemplo.invalid/token"),
        ("refresh_token", ""),
        ("client_id", None),
        ("type", "service_account"),
        ("scopes", None),
        ("expiry", "fecha inválida"),
    ],
)
def test_campos_invalidos_no_se_envian_a_red(
    configuracion: Config, token: Objeto, mocker: MockerFixture, campo: str, valor: ValorJson
) -> None:
    token[campo] = valor
    guardar_json(configuracion.token_path, token)
    enviar = mocker.patch.object(HTTPAdapter, "send")
    with pytest.raises((AutenticacionError, AutorizacionError)):
        ProveedorCredencialesGoogle(configuracion).cargar()
    enviar.assert_not_called()


def test_permission_error_no_se_interpreta_como_token_ausente(
    configuracion: Config, mocker: MockerFixture
) -> None:
    mocker.patch.object(Path, "read_text", side_effect=PermissionError("secreto"))
    with pytest.raises(PersistenciaError):
        ProveedorCredencialesGoogle(configuracion).cargar()


def test_cuenta_servicio_no_lee_ni_guarda_token_de_usuario(
    configuracion: Config, token: Objeto, mocker: MockerFixture
) -> None:
    configuracion = configuracion.model_copy(
        update={"google_tipo_autenticacion": "cuenta_servicio"}
    )
    guardar_json(
        configuracion.credentials_path,
        {
            "type": "service_account",
            "token_uri": modulo.URI_TOKEN,
            "client_email": "ficticio@example.invalid",
            "private_key": "clave_ficticia",
        },
    )
    existente = credenciales(token)
    fabricar = mocker.patch.object(
        CredencialesServicio, "from_service_account_info", return_value=existente
    )
    assert ProveedorCredencialesGoogle(configuracion).cargar(persistir=True) is existente
    assert fabricar.call_args.kwargs["scopes"] == configuracion.google_alcances
    assert not configuracion.token_path.exists()
    with pytest.raises(ConfiguracionError):
        ProveedorCredencialesGoogle(configuracion).autorizar()


def test_cuenta_servicio_real_con_firmante_ficticio_renueva_con_http_simulado(
    configuracion: Config, mocker: MockerFixture
) -> None:
    class FirmanteFicticio(Signer):
        @property
        def key_id(self) -> str:
            return "clave_ficticia"

        def sign(self, message: bytes) -> bytes:
            return b"firma_ficticia"

    configuracion = configuracion.model_copy(
        update={"google_tipo_autenticacion": "cuenta_servicio"}
    )
    guardar_json(
        configuracion.credentials_path,
        {
            "type": "service_account",
            "token_uri": modulo.URI_TOKEN,
            "client_email": "ficticio@example.invalid",
            "private_key": "clave_ficticia",
        },
    )
    mocker.patch("google.auth._service_account_info.from_dict", return_value=FirmanteFicticio())
    enviar = mocker.patch.object(HTTPAdapter, "send", return_value=respuesta())
    resultado = ProveedorCredencialesGoogle(configuracion).cargar()
    assert resultado.valid
    enviar.assert_called_once()
    assert not configuracion.token_path.exists()


@pytest.mark.parametrize("caso", ["tipo", "endpoint", "clave"])
def test_cuenta_servicio_invalida_no_envia_secretos(
    configuracion: Config, mocker: MockerFixture, caso: str
) -> None:
    configuracion = configuracion.model_copy(
        update={"google_tipo_autenticacion": "cuenta_servicio"}
    )
    documento: Objeto = {
        "type": "service_account",
        "token_uri": modulo.URI_TOKEN,
        "client_email": "ficticio@example.invalid",
        "private_key": "clave_ficticia",
    }
    if caso == "tipo":
        documento["type"] = "authorized_user"
    elif caso == "endpoint":
        documento["token_uri"] = "https://ejemplo.invalid/token"
    guardar_json(configuracion.credentials_path, documento)
    enviar = mocker.patch.object(HTTPAdapter, "send")
    with pytest.raises(AutenticacionError):
        ProveedorCredencialesGoogle(configuracion).cargar()
    enviar.assert_not_called()


@pytest.mark.parametrize("persistir", [False, True])
def test_oauth_inicial_es_explicito_loopback_pkce_y_sin_print(
    configuracion: Config,
    token: Objeto,
    mocker: MockerFixture,
    capsys: pytest.CaptureFixture[str],
    persistir: bool,
) -> None:
    escribir_cliente(configuracion)
    resultado = credenciales(token)
    flujo = mocker.Mock()
    flujo.run_local_server.return_value = resultado
    fabricar = mocker.patch.object(InstalledAppFlow, "from_client_config", return_value=flujo)
    assert ProveedorCredencialesGoogle(configuracion).autorizar(persistir=persistir) is resultado
    assert fabricar.call_args.kwargs["autogenerate_code_verifier"] is True
    opciones = flujo.run_local_server.call_args.kwargs
    assert opciones["host"] == opciones["bind_addr"] == "127.0.0.1"
    assert opciones["port"] == 0
    assert opciones["authorization_prompt_message"] == ""
    assert opciones["timeout_seconds"] == configuracion.google_timeout_autorizacion_segundos
    assert opciones["open_browser"] is True
    flujo.oauth2session.close.assert_called_once()
    assert configuracion.token_path.exists() is persistir
    assert capsys.readouterr().out == ""


def test_oauth_fallido_preserva_token_y_cierra_sesion(
    configuracion: Config, token: Objeto, mocker: MockerFixture
) -> None:
    escribir_cliente(configuracion)
    guardar_json(configuracion.token_path, token)
    original = configuracion.token_path.read_bytes()
    flujo = mocker.Mock()
    flujo.run_local_server.side_effect = RuntimeError("código secreto")
    mocker.patch.object(InstalledAppFlow, "from_client_config", return_value=flujo)
    with pytest.raises(AutenticacionError) as error:
        ProveedorCredencialesGoogle(configuracion).autorizar()
    assert "secreto" not in str(error.value)
    flujo.oauth2session.close.assert_called_once()
    assert configuracion.token_path.read_bytes() == original


@pytest.mark.parametrize("caso", ["auth_uri", "token_uri", "cliente_web"])
def test_oauth_rechaza_endpoints_ajenos_y_clientes_web(
    configuracion: Config, mocker: MockerFixture, caso: str
) -> None:
    escribir_cliente(configuracion)
    documento = cast(Objeto, leer_json(configuracion.credentials_path))
    instalado = cast(Objeto, documento["installed"])
    if caso == "cliente_web":
        documento = {"web": instalado}
    else:
        instalado[caso] = "https://ejemplo.invalid/endpoint"
    guardar_json(configuracion.credentials_path, documento)
    fabricar = mocker.patch.object(InstalledAppFlow, "from_client_config")
    with pytest.raises((AutenticacionError, EstructuraError)):
        ProveedorCredencialesGoogle(configuracion).autorizar()
    fabricar.assert_not_called()


@pytest.mark.parametrize(
    ("causa", "intentos"),
    [(ConnectTimeout("secreto"), 3), (Timeout("secreto"), 1), (ConnectionError("secreto"), 1)],
)
def test_codigo_de_un_solo_uso_no_se_reenvia_si_resultado_es_incierto(
    configuracion: Config, mocker: MockerFixture, causa: Exception, intentos: int
) -> None:
    enviar = mocker.patch.object(HTTPAdapter, "send", side_effect=causa)
    peticion = Solicitud("POST", modulo.URI_TOKEN, data="grant_type=authorization_code").prepare()
    with pytest.raises(TransitorioError):
        modulo._AdaptadorToken(configuracion, intercambio=True).send(peticion)
    assert enviar.call_count == intentos


@pytest.mark.parametrize("estado", [301, 400, 429, 500])
def test_intercambio_no_reintenta_respuestas_http(
    configuracion: Config, mocker: MockerFixture, estado: int
) -> None:
    enviar = mocker.patch.object(
        HTTPAdapter, "send", return_value=respuesta(estado, error="secreto")
    )
    peticion = Solicitud("POST", modulo.URI_TOKEN).prepare()
    with pytest.raises((AutenticacionError, TransitorioError)):
        modulo._AdaptadorToken(configuracion, intercambio=True).send(peticion)
    enviar.assert_called_once()


def test_adaptador_rechaza_otros_metodos_y_hosts(
    configuracion: Config, mocker: MockerFixture
) -> None:
    enviar = mocker.patch.object(HTTPAdapter, "send")
    for metodo, url in (("GET", modulo.URI_TOKEN), ("POST", "https://ejemplo.invalid/token")):
        with pytest.raises(AutenticacionError):
            modulo._AdaptadorToken(configuracion, intercambio=False).send(
                Solicitud(metodo, url).prepare()
            )
    enviar.assert_not_called()


def test_logs_no_exponen_tokens_claves_o_codigos_y_restauran_sdk(
    configuracion: Config, token: Objeto, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
) -> None:
    escribir_cliente(configuracion)
    logger = logging.getLogger("google_auth_oauthlib.flow")
    anterior = logger.disabled
    flujo = mocker.Mock()

    def autorizar(**opciones: object) -> Credentials:
        logger.warning("código secreto_ficticio")
        return credenciales(token)

    flujo.run_local_server.side_effect = autorizar
    mocker.patch.object(InstalledAppFlow, "from_client_config", return_value=flujo)
    with caplog.at_level("DEBUG"):
        ProveedorCredencialesGoogle(configuracion).autorizar()
    assert logger.disabled == anterior
    assert "secreto_ficticio" not in caplog.text
    assert "acceso_ficticio" not in caplog.text
    assert "renovacion_ficticia" not in caplog.text
    assert str(configuracion.token_path) not in caplog.text
    assert "Autorización de Google completada" in caplog.text


@pytest.mark.parametrize("campo", ["excel_path", "whatsapp_manifiesto_path"])
def test_token_no_puede_ocupar_archivo_de_datos(configuracion: Config, campo: str) -> None:
    with pytest.raises(ConfiguracionError):
        ProveedorCredencialesGoogle(
            configuracion.model_copy(update={"token_path": getattr(configuracion, campo)})
        )


@pytest.mark.parametrize("alcances", [(), (ALCANCE, ALCANCE), ("inventado",)])
def test_alcances_de_configuracion_deben_ser_validos(
    tmp_path: Path, alcances: tuple[str, ...]
) -> None:
    with pytest.raises(ValidationError):
        Config(directorio_base=tmp_path, google_alcances=alcances, _env_file=None)


def test_configuracion_carga_alcances_y_perfil_desde_entorno(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AC_GOOGLE_TIPO_AUTENTICACION", "cuenta_servicio")
    monkeypatch.setenv(
        "AC_GOOGLE_ALCANCES",
        json.dumps([ALCANCE, "https://www.googleapis.com/auth/drive.readonly"]),
    )
    configuracion = cargar_config(ruta_env=None)
    assert configuracion.google_tipo_autenticacion == "cuenta_servicio"
    assert len(configuracion.google_alcances) == 2


def test_configuracion_normaliza_espacios_de_alcances(tmp_path: Path) -> None:
    configuracion = Config(
        directorio_base=tmp_path, google_alcances=(" " + ALCANCE + " ",), _env_file=None
    )
    assert configuracion.google_alcances == (ALCANCE,)


@pytest.mark.parametrize("acceso", [123, "", "   "])
@pytest.mark.parametrize("renovada", [False, True])
def test_acceso_invalido_no_se_devuelve_ni_sobrescribe_token(
    configuracion: Config,
    token: Objeto,
    mocker: MockerFixture,
    acceso: ValorJson,
    renovada: bool,
) -> None:
    if renovada:
        token["expiry"] = "2000-01-01T00:00:00Z"
    else:
        token["token"] = acceso
    guardar_json(configuracion.token_path, token)
    original = configuracion.token_path.read_bytes()
    mocker.patch.object(
        HTTPAdapter, "send", return_value=respuesta(access_token=acceso, expires_in=3600)
    )
    with pytest.raises(AutenticacionError):
        ProveedorCredencialesGoogle(configuracion).cargar(persistir=True)
    assert configuracion.token_path.read_bytes() == original


def test_respuesta_de_renovacion_con_alcance_malformado_se_traduce(
    configuracion: Config, token: Objeto, mocker: MockerFixture
) -> None:
    token["expiry"] = "2000-01-01T00:00:00Z"
    guardar_json(configuracion.token_path, token)
    original = configuracion.token_path.read_bytes()
    mocker.patch.object(
        HTTPAdapter,
        "send",
        return_value=respuesta(access_token="ficticio", expires_in=3600, scope=[ALCANCE]),
    )
    with pytest.raises(AutenticacionError):
        ProveedorCredencialesGoogle(configuracion).cargar(persistir=True)
    assert configuracion.token_path.read_bytes() == original


def test_cierre_fallido_no_oculta_error_oauth_original(
    configuracion: Config, mocker: MockerFixture
) -> None:
    escribir_cliente(configuracion)
    flujo = mocker.Mock()
    flujo.run_local_server.side_effect = RuntimeError("fallo ficticio")
    flujo.oauth2session.close.side_effect = OSError("fallo ficticio al cerrar")
    mocker.patch.object(InstalledAppFlow, "from_client_config", return_value=flujo)
    with pytest.raises(AutenticacionError) as error:
        ProveedorCredencialesGoogle(configuracion).autorizar()
    assert isinstance(error.value.__cause__, RuntimeError)
    assert not configuracion.token_path.exists()


@pytest.mark.parametrize("renovar", [False, True])
def test_credenciales_cargadas_autorizan_lector_sheets_real_con_http_simulado(
    configuracion: Config, token: Objeto, mocker: MockerFixture, renovar: bool
) -> None:
    configuracion = configuracion.model_copy(update={"spreadsheet_id": "documento_sintetico"})
    if renovar:
        token["expiry"] = "2000-01-01T00:00:00Z"
    guardar_json(configuracion.token_path, token)
    documento = {
        "spreadsheetId": "documento_sintetico",
        "sheets": [
            {
                "properties": {
                    "title": nombre,
                    "sheetId": indice,
                    "gridProperties": {"rowCount": 2, "columnCount": 5},
                },
                "data": [
                    {"rowData": [{"values": [{"userEnteredValue": {"stringValue": "Cliente"}}]}]}
                ],
            }
            for indice, nombre in enumerate(("Inviu", "IOL", "Bull", "Balanz", "PPI"))
        ],
    }
    respuesta_sheets = respuesta(
        spreadsheetId=documento["spreadsheetId"], sheets=documento["sheets"]
    )
    enviar = mocker.patch.object(
        HTTPAdapter,
        "send",
        side_effect=[respuesta(), respuesta_sheets] if renovar else [respuesta_sheets],
    )
    credencial = ProveedorCredencialesGoogle(configuracion).cargar()
    with crear_lector_sheets(configuracion, credencial) as lector:
        assert len(lector.revision()) == 64
        assert len(lector.listar_hojas()) == 5
    assert enviar.call_count == (2 if renovar else 1)
    solicitud = enviar.call_args.args[0]
    assert solicitud.method == "GET"
    assert solicitud.headers["Authorization"] == f"Bearer {credencial.token}"
