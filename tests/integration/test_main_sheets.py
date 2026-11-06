"""Comando Sheets con SDK real, HTTP simulado y credenciales sintéticas temporales."""

import json
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from os import environ
from pathlib import Path
from typing import cast

import pytest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from pytest_mock import MockerFixture
from requests import PreparedRequest, Response
from requests.adapters import HTTPAdapter
from requests.exceptions import Timeout

from automatizacion_clientes import main as entrada
from automatizacion_clientes.config import Config
from automatizacion_clientes.core.errores import AutorizacionError, PersistenciaError
from automatizacion_clientes.core.modelos import ResolucionSemanal
from automatizacion_clientes.infra.almacen_aliases_json import AlmacenAliasesJson
from automatizacion_clientes.infra.almacen_catalogo_json import AlmacenCatalogoJson
from automatizacion_clientes.infra.almacen_resoluciones_json import AlmacenResolucionesJson
from automatizacion_clientes.infra.almacenamiento_json import guardar_json
from automatizacion_clientes.infra.autenticacion_google import ProveedorCredencialesGoogle
from automatizacion_clientes.infra.escritor_clientes_sheets import EscritorClientesSheets
from automatizacion_clientes.infra.lector_clientes_sheets import (
    LectorClientesSheets,
    crear_lector_sheets,
)
from automatizacion_clientes.infra.lector_contactos_json import LectorContactosJson

ALCANCE = "https://www.googleapis.com/auth/spreadsheets"
ALCANCE_LECTURA = "https://www.googleapis.com/auth/spreadsheets.readonly"
type Objeto = dict[str, object]


def objeto(valor: object) -> Objeto:
    return cast(Objeto, valor)


def lista(valor: object) -> list[Objeto]:
    return cast(list[Objeto], valor)


def hoja(documento: Objeto, nombre: str) -> Objeto:
    return next(
        destino
        for destino in lista(documento["sheets"])
        if objeto(destino["properties"])["title"] == nombre
    )


def celda(destino: Objeto, fila: int, columna: int) -> Objeto:
    filas = lista(lista(destino["data"])[0]["rowData"])
    while len(filas) < fila:
        filas.append({"values": []})
    valores = lista(filas[fila - 1]["values"])
    while len(valores) < columna:
        valores.append({})
    return valores[columna - 1]


def respuesta(documento: object, estado: int = 200) -> Response:
    resultado = Response()
    resultado.status_code = estado
    resultado.encoding = "utf-8"
    resultado._content = json.dumps(documento).encode("utf-8")
    _ = resultado.content
    return resultado


class ApiSimulada:
    def __init__(self) -> None:
        self.documento: Objeto = {"spreadsheetId": "documento_sintetico", "sheets": []}
        self.lotes: list[Objeto] = []
        self.consultas = 0
        self.renovaciones = 0
        self.alcance = ALCANCE
        self.fallar_tras_hoja: str | None = None
        self.estado_lectura = 200
        for indice, nombre in enumerate(("Inviu", "IOL", "Bull", "Balanz", "PPI")):
            destino: Objeto = {
                "properties": {
                    "title": nombre,
                    "sheetId": indice,
                    "gridProperties": {"rowCount": 100, "columnCount": 8},
                },
                "data": [{"rowData": []}],
            }
            celda(destino, 1, 1)["userEnteredValue"] = {"stringValue": "Cliente"}
            celda(destino, 2, 4 if nombre == "Balanz" else 3)["userEnteredValue"] = {
                "stringValue": "Anterior"
            }
            celda(destino, 3, 1)["userEnteredValue"] = {"stringValue": "Axa Prueba"}
            celda(destino, 5, 1)["userEnteredValue"] = {"stringValue": "Notuka Ejemplo"}
            celda(destino, 3, 2)["userEnteredValue"] = {"formulaValue": "=100+200"}
            if nombre == "IOL":
                celda(destino, 8, 1)["userEnteredValue"] = {
                    "stringValue": "AXA PRUEBA Y/O NOTUKA EJEMPLO"
                }
            lista(self.documento["sheets"]).append(destino)

    def enviar(self, request: PreparedRequest, **opciones: object) -> Response:
        assert opciones["timeout"] is not None
        if request.url == "https://oauth2.googleapis.com/token":
            assert request.method == "POST"
            self.renovaciones += 1
            return respuesta(
                {
                    "access_token": "acceso_renovado_ficticio",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                    "scope": self.alcance,
                }
            )
        assert request.url is not None
        assert request.url.startswith(
            "https://sheets.googleapis.com/v4/spreadsheets/documento_sintetico"
        )
        assert request.headers["authorization"] in {
            "Bearer acceso_ficticio",
            "Bearer acceso_renovado_ficticio",
        }
        if request.method == "GET":
            self.consultas += 1
            return respuesta(
                self.documento if self.estado_lectura == 200 else {"error": "ficticio"},
                self.estado_lectura,
            )
        assert request.method == "POST"
        assert request.url.endswith(":batchUpdate")
        cuerpo = request.body
        assert isinstance(cuerpo, (str, bytes))
        contenido = objeto(json.loads(cuerpo))
        self.lotes.append(contenido)
        nombre = ""
        for peticion in lista(contenido["requests"]):
            actualizacion = objeto(peticion["updateCells"])
            inicio = objeto(actualizacion["start"])
            destino = next(
                destino
                for destino in lista(self.documento["sheets"])
                if objeto(destino["properties"])["sheetId"] == inicio["sheetId"]
            )
            nombre = cast(str, objeto(destino["properties"])["title"])
            for desplazamiento, fila in enumerate(lista(actualizacion["rows"]), 1):
                valor = lista(fila["values"])[0]
                actual = celda(
                    destino,
                    cast(int, inicio["rowIndex"]) + desplazamiento,
                    cast(int, inicio["columnIndex"]) + 1,
                )
                actual.pop("userEnteredValue", None)
                actual.pop("effectiveValue", None)
                if "userEnteredValue" in valor:
                    actual["userEnteredValue"] = deepcopy(valor["userEnteredValue"])
                if "userEnteredFormat" in valor:
                    formato = objeto(actual.setdefault("userEnteredFormat", {}))
                    formato.update(deepcopy(objeto(valor["userEnteredFormat"])))
                    actual["effectiveFormat"] = deepcopy(formato)
        if self.fallar_tras_hoja == nombre:
            raise Timeout("ficticio después de aplicar")
        return respuesta({"spreadsheetId": "documento_sintetico"})


@dataclass
class Escenario:
    configuracion: Config
    api: ApiSimulada

    def argumentos(self, *opciones: str) -> list[str]:
        return [
            "--sin-env",
            "--directorio-base",
            str(self.configuracion.directorio_base),
            "--encabezado",
            "2026-11-02 / 2026-11-06",
            *opciones,
        ]


@pytest.fixture(autouse=True)
def aislar_entorno(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture) -> None:
    for nombre in tuple(environ):
        if nombre.upper().startswith("AC_"):
            monkeypatch.delenv(nombre)
    monkeypatch.chdir(tmp_path)
    mocker.patch("requests.sessions.get_netrc_auth", return_value=None)
    mocker.patch(
        "webbrowser.open", side_effect=AssertionError("Navegador no autorizado por esta prueba")
    )


def escribir_contactos(configuracion: Config, nombre: str = "Axa Prueba (IOL/Balanz)") -> None:
    guardar_json(
        configuracion.contactos_path,
        {
            "rango": {"desde": "2026-11-02", "hasta": "2026-11-06"},
            "contactos": [{"nombre_crudo": nombre, "ultima_fecha": "2026-11-05"}],
        },
    )


def escribir_token(configuracion: Config, *, vencido: bool = False, alcance: str = ALCANCE) -> None:
    guardar_json(
        configuracion.token_path,
        {
            "token": "acceso_ficticio",
            "refresh_token": "renovacion_ficticia",
            "client_id": "cliente_ficticio",
            "client_secret": "secreto_ficticio",
            "token_uri": "https://oauth2.googleapis.com/token",
            "scopes": [alcance],
            "expiry": "2000-01-01T00:00:00Z" if vencido else "2099-01-01T00:00:00Z",
        },
    )


def archivos(base: Path) -> dict[Path, bytes]:
    return {ruta.relative_to(base): ruta.read_bytes() for ruta in base.rglob("*") if ruta.is_file()}


@pytest.fixture
def escenario(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture) -> Escenario:
    monkeypatch.setenv("AC_REPOSITORIO", "sheets")
    monkeypatch.setenv("AC_SPREADSHEET_ID", "documento_sintetico")
    monkeypatch.setenv("AC_GOOGLE_ALCANCES", json.dumps([ALCANCE]))
    monkeypatch.setenv("AC_REINTENTO_ESPERA_MINIMA_SEGUNDOS", "0.001")
    monkeypatch.setenv("AC_REINTENTO_ESPERA_MAXIMA_SEGUNDOS", "0.002")
    configuracion = Config(directorio_base=tmp_path, _env_file=None)
    escribir_contactos(configuracion)
    escribir_token(configuracion)
    api = ApiSimulada()
    mocker.patch.object(HTTPAdapter, "send", side_effect=api.enviar)
    return Escenario(configuracion, api)


@pytest.mark.parametrize("vencido", [False, True])
def test_simulacion_no_guarda_archivos_ni_construye_escritor(
    escenario: Escenario,
    mocker: MockerFixture,
    capsys: pytest.CaptureFixture[str],
    vencido: bool,
) -> None:
    configuracion = escenario.configuracion
    escribir_token(configuracion, vencido=vencido)
    originales = archivos(configuracion.directorio_base)
    documento = deepcopy(escenario.api.documento)
    fabrica = mocker.patch.object(entrada, "crear_escritor_sheets")
    assert entrada.main(escenario.argumentos()) == 0
    fabrica.assert_not_called()
    assert archivos(configuracion.directorio_base) == originales
    assert escenario.api.documento == documento
    assert not escenario.api.lotes
    assert escenario.api.renovaciones == int(vencido)
    salida = capsys.readouterr()
    assert salida.out == ""
    assert "Plan de hoja: IOL; columna=4" in salida.err
    for secreto in (
        "Axa Prueba",
        "secreto_ficticio",
        "acceso_ficticio",
        "documento_sintetico",
        str(configuracion.token_path),
    ):
        assert secreto not in salida.err


def test_aplicar_conecta_todo_y_repetir_no_crea_otro_respaldo(
    escenario: Escenario,
    mocker: MockerFixture,
) -> None:
    configuracion = escenario.configuracion
    original = deepcopy(escenario.api.documento)
    leer_contactos = mocker.spy(LectorContactosJson, "leer")
    assert entrada.main(escenario.argumentos("--aplicar")) == 0
    leer_contactos.assert_called_once()
    respaldos = list(configuracion.directorio_respaldos.iterdir())
    assert len(respaldos) == 1
    assert objeto(json.loads(respaldos[0].read_text(encoding="utf-8")))["documento"] == original
    catalogo = AlmacenCatalogoJson(configuracion.catalogo_path).leer()
    assert catalogo is not None
    assert catalogo.identificador_origen == "sheets:documento_sintetico"
    assert len(AlmacenResolucionesJson(configuracion.resolucion_path).leer().resueltos) == 1
    assert (
        AlmacenAliasesJson(configuracion.aliases_path).buscar_por_nombre("Axa Prueba (IOL/Balanz)")
        is not None
    )
    credenciales = ProveedorCredencialesGoogle(configuracion).cargar()
    with crear_lector_sheets(configuracion, credenciales) as lector:
        assert lector.leer_marcas("IOL", 4) == frozenset({3, 8})
        assert lector.leer_marcas("Balanz", 5) == frozenset({3})
        assert lector.leer_marcas("Bull", 4) == frozenset()
    assert celda(hoja(escenario.api.documento, "IOL"), 3, 2) == celda(hoja(original, "IOL"), 3, 2)
    assert len(escenario.api.lotes) == 5
    assert entrada.main(escenario.argumentos("--aplicar")) == 0
    assert len(escenario.api.lotes) == 5
    assert list(configuracion.directorio_respaldos.iterdir()) == respaldos


def test_token_renovado_se_guarda_en_modo_real(escenario: Escenario) -> None:
    escribir_token(escenario.configuracion, vencido=True)
    assert entrada.main(escenario.argumentos("--aplicar")) == 0
    assert escenario.api.renovaciones == 1
    token = objeto(json.loads(escenario.configuracion.token_path.read_text(encoding="utf-8")))
    assert token["token"] == "acceso_renovado_ficticio"


@pytest.mark.parametrize("aplicar", [False, True])
def test_solo_lectura_simula_y_rechaza_aplicar_antes_de_autenticar(
    escenario: Escenario,
    monkeypatch: pytest.MonkeyPatch,
    mocker: MockerFixture,
    aplicar: bool,
) -> None:
    monkeypatch.setenv("AC_GOOGLE_ALCANCES", json.dumps([ALCANCE_LECTURA]))
    escribir_token(escenario.configuracion, alcance=ALCANCE_LECTURA)
    carga = mocker.spy(ProveedorCredencialesGoogle, "cargar")
    originales = archivos(escenario.configuracion.directorio_base)
    assert entrada.main(escenario.argumentos(*(["--aplicar"] if aplicar else []))) == (
        2 if aplicar else 0
    )
    assert carga.call_count == (0 if aplicar else 1)
    assert not escenario.api.lotes
    assert archivos(escenario.configuracion.directorio_base) == originales


@pytest.mark.parametrize("aplicar", [False, True])
def test_pendientes_bloquean_marcas_y_en_real_guardan_informe(
    escenario: Escenario,
    aplicar: bool,
) -> None:
    configuracion = escenario.configuracion
    escribir_contactos(configuracion, "Identidad inexistente zzz 987")
    assert entrada.main(escenario.argumentos(*(["--aplicar"] if aplicar else []))) == 3
    assert not escenario.api.lotes
    assert not configuracion.directorio_respaldos.exists()
    assert not configuracion.aliases_path.exists()
    assert configuracion.catalogo_path.exists() is aplicar
    assert configuracion.resolucion_path.exists() is aplicar


def test_permitir_pendientes_aplica_solo_resueltos(escenario: Escenario) -> None:
    escribir_contactos(escenario.configuracion, "Identidad inexistente zzz 987")
    assert entrada.main(escenario.argumentos("--aplicar", "--permitir-pendientes")) == 0
    assert len(escenario.api.lotes) == 5
    assert not escenario.configuracion.aliases_path.exists()


@pytest.mark.parametrize("simular", [False, True])
def test_modo_del_entorno_y_override_se_respetan(
    escenario: Escenario,
    monkeypatch: pytest.MonkeyPatch,
    simular: bool,
) -> None:
    monkeypatch.setenv("AC_SIMULACION", "false")
    assert entrada.main(escenario.argumentos(*(["--simular"] if simular else []))) == 0
    assert bool(escenario.api.lotes) is not simular


def test_token_ausente_no_inicia_oauth_automaticamente(
    escenario: Escenario,
    mocker: MockerFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    escenario.configuracion.token_path.unlink()
    autorizar = mocker.spy(ProveedorCredencialesGoogle, "autorizar")
    assert entrada.main(escenario.argumentos()) == 4
    autorizar.assert_not_called()
    assert escenario.api.consultas == 0
    assert "--autorizar-google" in capsys.readouterr().err


def test_fallo_de_contactos_cierra_lector_y_escritor(
    escenario: Escenario,
    mocker: MockerFixture,
) -> None:
    escenario.configuracion.contactos_path.write_bytes(b"{invalido")
    lector = mocker.spy(LectorClientesSheets, "cerrar")
    escritor = mocker.spy(EscritorClientesSheets, "cerrar")
    assert entrada.main(escenario.argumentos("--aplicar")) == 2
    escritor.assert_called_once()
    assert lector.call_count == 2  # lector del flujo e interno del escritor
    assert not escenario.api.lotes


def test_fallo_del_informe_impide_respaldos_y_marcas(
    escenario: Escenario,
    mocker: MockerFixture,
) -> None:
    mocker.patch.object(
        AlmacenResolucionesJson, "guardar", side_effect=PersistenciaError("ficticio")
    )
    assert entrada.main(escenario.argumentos("--aplicar")) == 4
    assert not escenario.api.lotes
    assert not escenario.configuracion.directorio_respaldos.exists()


def test_cambio_tras_guardar_informe_detiene_registro(
    escenario: Escenario,
    mocker: MockerFixture,
) -> None:
    guardar = AlmacenResolucionesJson.guardar

    def guardar_y_cambiar(self: AlmacenResolucionesJson, resolucion: ResolucionSemanal) -> None:
        guardar(self, resolucion)
        celda(hoja(escenario.api.documento, "IOL"), 3, 1)["userEnteredValue"] = {
            "stringValue": "Otra Persona"
        }

    mocker.patch.object(
        AlmacenResolucionesJson, "guardar", autospec=True, side_effect=guardar_y_cambiar
    )
    assert entrada.main(escenario.argumentos("--aplicar")) == 5
    assert not escenario.api.lotes
    assert not escenario.configuracion.directorio_respaldos.exists()


def test_respuesta_perdida_despues_de_aplicar_devuelve_seis_y_conserva_respaldo(
    escenario: Escenario,
    capsys: pytest.CaptureFixture[str],
) -> None:
    escenario.api.fallar_tras_hoja = "IOL"
    assert entrada.main(escenario.argumentos("--aplicar")) == 6
    assert len(escenario.api.lotes) == 2
    assert len(list(escenario.configuracion.directorio_respaldos.iterdir())) == 1
    salida = capsys.readouterr()
    assert "1 hojas confirmadas" in salida.err
    assert "hoja en curso=True" in salida.err
    assert "Traceback" not in salida.err


@pytest.mark.parametrize("estado", [401, 403, 404])
def test_error_http_se_traduce_sin_reintentos_de_permiso(
    escenario: Escenario,
    estado: int,
) -> None:
    escenario.api.estado_lectura = estado
    assert entrada.main(escenario.argumentos()) == 4
    assert escenario.api.consultas == 1
    assert not escenario.api.lotes


def test_cache_invalida_se_repara_solo_con_opcion_explicita(escenario: Escenario) -> None:
    configuracion = escenario.configuracion
    configuracion.catalogo_path.parent.mkdir(parents=True)
    configuracion.catalogo_path.write_bytes(b"{invalido")
    assert entrada.main(escenario.argumentos()) == 2
    assert entrada.main(escenario.argumentos("--reconstruir-catalogo")) == 0
    assert configuracion.catalogo_path.read_bytes() == b"{invalido"
    assert entrada.main(escenario.argumentos("--reconstruir-catalogo", "--aplicar")) == 0
    assert AlmacenCatalogoJson(configuracion.catalogo_path).leer() is not None


def test_autorizacion_comando_guarda_token_sin_ejecutar_sheets(
    escenario: Escenario,
    mocker: MockerFixture,
) -> None:
    configuracion = escenario.configuracion
    guardar_json(
        configuracion.credentials_path,
        {
            "installed": {
                "auth_uri": "https://accounts.google.com/o/oauth2/v2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_id": "cliente_ficticio",
                "client_secret": "secreto_ficticio",
                "redirect_uris": ["http://localhost"],
            }
        },
    )
    fabricar = cast(
        Callable[[Mapping[str, object]], Credentials], Credentials.from_authorized_user_info
    )
    credenciales = fabricar(
        objeto(json.loads(configuracion.token_path.read_text(encoding="utf-8")))
    )
    flujo = mocker.Mock()
    flujo.run_local_server.return_value = credenciales
    mocker.patch.object(InstalledAppFlow, "from_client_config", return_value=flujo)
    assert (
        entrada.main(
            [
                "--sin-env",
                "--directorio-base",
                str(configuracion.directorio_base),
                "--autorizar-google",
            ]
        )
        == 0
    )
    flujo.run_local_server.assert_called_once()
    flujo.oauth2session.close.assert_called_once()
    assert escenario.api.consultas == 0
    assert not escenario.api.lotes
    assert not configuracion.resolucion_path.exists()


def test_fallo_al_construir_escritor_cierra_lector_previo(
    escenario: Escenario,
    mocker: MockerFixture,
) -> None:
    cierre = mocker.spy(LectorClientesSheets, "cerrar")
    mocker.patch.object(entrada, "crear_escritor_sheets", side_effect=AutorizacionError("ficticio"))
    assert entrada.main(escenario.argumentos("--aplicar")) == 4
    cierre.assert_called_once()
    assert escenario.api.consultas == 0
    assert not escenario.api.lotes


def test_autorizacion_interactiva_no_aplica_a_cuenta_servicio(
    escenario: Escenario,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AC_GOOGLE_TIPO_AUTENTICACION", "cuenta_servicio")
    original = archivos(escenario.configuracion.directorio_base)
    assert (
        entrada.main(
            [
                "--sin-env",
                "--directorio-base",
                str(escenario.configuracion.directorio_base),
                "--autorizar-google",
            ]
        )
        == 2
    )
    assert archivos(escenario.configuracion.directorio_base) == original
    assert escenario.api.consultas == 0
