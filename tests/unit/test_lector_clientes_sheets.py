"""Instantáneas de Sheets, colores y errores HTTP sin conexiones ni credenciales reales."""

from collections.abc import Callable, Mapping
from copy import deepcopy
from os import environ
from pathlib import Path
from typing import cast

import pytest
from google.auth.credentials import AnonymousCredentials, Credentials
from google.auth.exceptions import GoogleAuthError, RefreshError, TransportError
from pytest_mock import MockerFixture
from requests import Response, Session
from requests.exceptions import (
    ChunkedEncodingError,
    ConnectionError,
    RequestException,
    SSLError,
    Timeout,
)

from automatizacion_clientes.config import Config
from automatizacion_clientes.core.contratos import LectorRegistroClientes
from automatizacion_clientes.core.errores import (
    AutenticacionError,
    AutorizacionError,
    ConfiguracionError,
    EstructuraError,
    ServicioExternoError,
    TiempoEsperaError,
    TransitorioError,
)
from automatizacion_clientes.infra import lector_clientes_sheets as modulo
from automatizacion_clientes.infra.lector_clientes_sheets import (
    LectorClientesSheets,
    crear_lector_sheets,
)

type Objeto = dict[str, object]
VERDE = {"red": 118 / 255, "green": 146 / 255, "blue": 60 / 255}


def error_google[TipoError: GoogleAuthError](tipo: type[TipoError]) -> TipoError:
    # El constructor externo no está tipado; la prueba usa solo su argumento público.
    fabricar = cast(Callable[[str], TipoError], tipo)
    return fabricar("secreto")


class Respuesta(Response):
    def __init__(self, documento: object, estado: int = 200) -> None:
        super().__init__()
        self.documento = documento
        self.status_code = estado
        self.cierres = 0

    def json(self, **kwargs: object) -> object:
        if isinstance(self.documento, Exception):
            raise self.documento
        return self.documento

    def close(self) -> None:
        self.cierres += 1


class Sesion:
    def __init__(self, *resultados: object) -> None:
        self.resultados = list(resultados)
        self.peticiones: list[tuple[str, Mapping[str, str | list[str]], float, bool]] = []
        self.cierres = 0

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str | list[str]],
        timeout: float,
        allow_redirects: bool,
    ) -> Response:
        self.peticiones.append((url, params, timeout, allow_redirects))
        resultado = self.resultados.pop(0) if len(self.resultados) > 1 else self.resultados[0]
        if isinstance(resultado, Exception):
            raise resultado
        return resultado if isinstance(resultado, Respuesta) else Respuesta(deepcopy(resultado))

    def close(self) -> None:
        self.cierres += 1


def texto(valor: str) -> Objeto:
    return {
        "userEnteredValue": {"stringValue": valor},
        "effectiveValue": {"stringValue": valor},
        "formattedValue": valor,
    }


def hoja(documento: Objeto, nombre: str = "IOL") -> Objeto:
    return next(
        elemento
        for elemento in cast(list[Objeto], documento["sheets"])
        if cast(Objeto, elemento["properties"])["title"] == nombre
    )


def celda(destino: Objeto, fila: int, columna: int, contenido: Objeto) -> None:
    cast(list[Objeto], destino["data"]).append(
        {
            "startRow": fila - 1,
            "startColumn": columna - 1,
            "rowData": [{"values": [contenido]}],
        }
    )


def encontrar_celda(documento: Objeto, fila: int, columna: int, nombre: str = "IOL") -> Objeto:
    bloque = next(
        bloque
        for bloque in cast(list[Objeto], hoja(documento, nombre)["data"])
        if bloque["startRow"] == fila - 1 and bloque["startColumn"] == columna - 1
    )
    filas = cast(list[Objeto], bloque["rowData"])
    return cast(list[Objeto], filas[0]["values"])[0]


@pytest.fixture
def documento() -> Objeto:
    hojas: list[Objeto] = []
    for indice, nombre in enumerate(("Inviu", "IOL", "Bull", "Balanz", "PPI")):
        destino: Objeto = {
            "properties": {
                "title": nombre,
                "sheetId": indice * 10,
                "sheetType": "GRID",
                "gridProperties": {"rowCount": 5005, "columnCount": 800},
            },
            "data": [],
        }
        celda(destino, 1, 1, texto("Cliente"))
        if nombre != "PPI":
            celda(destino, 2, 4 if nombre == "Balanz" else 3, texto("02 al 06-11"))
            celda(destino, 3, 1, texto("Axa Prueba"))
        hojas.append(destino)
    resultado: Objeto = {"spreadsheetId": "documento_sintetico", "sheets": hojas}
    destino = hoja(resultado)
    encontrar_celda(resultado, 3, 1)["userEnteredValue"] = {"stringValue": "  Axa Prueba  "}
    celda(destino, 2, 4, texto(" 09 al 13-11 "))
    celda(destino, 5, 1, texto("Notuka Ejemplo"))
    celda(destino, 6, 1, texto("   "))
    celda(destino, 3005, 1, texto("AXA PRUEBA Y/O NOTUKA EJEMPLO"))
    celda(destino, 3, 4, {"effectiveFormat": {"backgroundColorStyle": {"rgbColor": VERDE}}})
    celda(
        destino, 5, 4, {"effectiveFormat": {"backgroundColor": {"red": 1, "green": 1, "blue": 1}}}
    )
    celda(destino, 3005, 4, texto(" X "))
    celda(destino, 4500, 4, {"effectiveFormat": {"backgroundColorStyle": {"rgbColor": VERDE}}})
    celda(hoja(resultado, "Balanz"), 2, 3, texto("Capital fijo"))
    return resultado


def lector(sesion: Sesion) -> LectorClientesSheets:
    return LectorClientesSheets(
        "documento_sintetico",
        sesion=sesion,
        espera_minima_segundos=0.001,
        espera_maxima_segundos=0.002,
    )


def test_constructor_y_consultas_reutilizan_instantanea_hasta_revision(documento: Objeto) -> None:
    sesion = Sesion(documento)
    lectura: LectorRegistroClientes = lector(sesion)
    assert not sesion.peticiones
    hojas = lectura.listar_hojas()
    assert tuple(hoja.nombre for hoja in hojas) == ("Inviu", "IOL", "Bull", "Balanz", "PPI")
    assert hojas[1].sheet_id == 10
    assert hojas[1].ultima_fila_con_cliente == 3005
    assert hojas[-1].ultima_fila_con_cliente == 2
    assert lectura.ultima_semana("PPI") == (1, "")
    assert lectura.leer_clientes("PPI") == ()
    assert lectura.leer_encabezados("IOL") == ((3, "02 al 06-11"), (4, " 09 al 13-11 "))
    assert lectura.ultima_semana("Balanz") == (4, "02 al 06-11")
    assert lectura.leer_marcas("IOL", 4) == frozenset({3, 3005})
    assert tuple(cliente.fila for cliente in lectura.leer_clientes("IOL")) == (3, 5, 3005)
    assert lectura.leer_clientes("IOL")[0].nombre_crudo == "  Axa Prueba  "
    assert len(sesion.peticiones) == 1
    url, parametros, timeout, redirecciones = sesion.peticiones[0]
    assert url.endswith("/documento_sintetico")
    assert parametros["ranges"] == ["'Inviu'", "'IOL'", "'Bull'", "'Balanz'", "'PPI'"]
    assert "effectiveFormat" not in str(parametros["fields"])  # data solicita todos sus campos.
    assert "data" in str(parametros["fields"])
    assert timeout == 30.0 and not redirecciones
    lectura.revision()
    assert len(sesion.peticiones) == 2


def test_nuevas_filas_y_columnas_superan_limites_del_lector_anterior(documento: Objeto) -> None:
    celda(hoja(documento), 2, 750, texto("Semana lejana"))
    celda(hoja(documento), 5000, 1, texto("Cliente posterior"))
    celda(hoja(documento), 5000, 750, texto("x"))
    lectura = lector(Sesion(documento))
    assert lectura.ultima_semana("IOL") == (750, "Semana lejana")
    assert lectura.leer_marcas("IOL", 750) == frozenset({5000})
    assert lectura.listar_hojas()[1].ultima_fila_con_cliente == 5000


@pytest.mark.parametrize("cambio", ["nombre", "formato", "regla", "tema"])
def test_revision_refresca_y_detecta_contenido_y_formato(documento: Objeto, cambio: str) -> None:
    sesion = Sesion(documento)
    lectura = lector(sesion)
    anterior = lectura.revision()
    if cambio == "nombre":
        encontrar_celda(documento, 5, 1)["userEnteredValue"] = {"stringValue": "Otra Persona"}
        assert lectura.leer_clientes("IOL")[1].nombre_crudo == "Notuka Ejemplo"
    elif cambio == "formato":
        encontrar_celda(documento, 5, 4)["userEnteredFormat"] = {"textFormat": {"bold": True}}
    elif cambio == "regla":
        hoja(documento)["conditionalFormats"] = [{"ranges": [{"sheetId": 10}]}]
    else:
        documento["properties"] = {"spreadsheetTheme": {"primaryFontFamily": "Arial"}}
    assert lectura.revision() != anterior
    if cambio == "nombre":
        assert lectura.leer_clientes("IOL")[1].nombre_crudo == "Otra Persona"


def test_orden_de_claves_json_no_modifica_revision(documento: Objeto) -> None:
    lectura = lector(Sesion(documento, dict(reversed(tuple(documento.items())))))
    assert lectura.revision() == lectura.revision()


def test_fallo_de_refresco_invalida_la_instantanea_anterior(documento: Objeto) -> None:
    sesion = Sesion(documento, {"spreadsheetId": "documento_sintetico", "sheets": []})
    lectura = lector(sesion)
    lectura.listar_hojas()
    with pytest.raises(EstructuraError):
        lectura.revision()
    with pytest.raises(EstructuraError):
        lectura.leer_clientes("IOL")
    assert len(sesion.peticiones) == 3


@pytest.mark.parametrize(
    ("columna", "valida"),
    [(True, False), (0, False), (-1, False), (2, False), (5, False), (3, True)],
)
def test_columnas_semanales_validas(documento: Objeto, columna: int, valida: bool) -> None:
    lectura = lector(Sesion(documento))
    if valida:
        assert lectura.leer_marcas("IOL", columna) == frozenset()
    else:
        with pytest.raises(EstructuraError):
            lectura.leer_marcas("IOL", columna)
    with pytest.raises(EstructuraError):
        lectura.leer_marcas("PPI", 3)


def test_cierre_idempotente_y_propiedad_de_la_sesion(documento: Objeto) -> None:
    sesion = Sesion(documento)
    lectura = lector(sesion)
    with lectura:
        lectura.listar_hojas()
    lectura.cerrar()
    assert sesion.cierres == 0
    with pytest.raises(ConfiguracionError):
        lectura.revision()
    with pytest.raises(ConfiguracionError):
        lectura.__enter__()
    propia = LectorClientesSheets("documento_sintetico", sesion=sesion, cerrar_sesion=True)
    with pytest.raises(RuntimeError), propia:
        raise RuntimeError("Fallo del bloque.")
    propia.cerrar()
    assert sesion.cierres == 1


def test_hoja_ajena_se_rechaza_sin_consultar_red(documento: Objeto) -> None:
    sesion = Sesion(documento)
    with pytest.raises(EstructuraError):
        lector(sesion).leer_clientes("No configurada")
    assert not sesion.peticiones


@pytest.mark.parametrize(
    ("campo", "valor"),
    [
        ("intentos", 0),
        ("intentos", True),
        ("intentos", 11),
        ("timeout_segundos", 0),
        ("timeout_segundos", float("inf")),
        ("espera_minima_segundos", -1),
        ("espera_maxima_segundos", float("nan")),
        ("espera_minima_segundos", 40),
    ],
)
def test_configuracion_invalida_no_realiza_io(campo: str, valor: float) -> None:
    sesion = Sesion({})
    with pytest.raises(ConfiguracionError):
        if campo == "intentos":
            assert isinstance(valor, int)
            LectorClientesSheets("documento", sesion=sesion, intentos=valor)
        elif campo == "timeout_segundos":
            LectorClientesSheets("documento", sesion=sesion, timeout_segundos=valor)
        elif campo == "espera_minima_segundos":
            LectorClientesSheets("documento", sesion=sesion, espera_minima_segundos=valor)
        else:
            LectorClientesSheets("documento", sesion=sesion, espera_maxima_segundos=valor)
    assert not sesion.peticiones


def test_identificador_vacio_y_codificacion_de_url() -> None:
    with pytest.raises(ConfiguracionError):
        LectorClientesSheets(" ", sesion=Sesion({}))
    sesion = Sesion({"spreadsheetId": "uno/dos?token=secreto", "sheets": []})
    with pytest.raises(EstructuraError):
        LectorClientesSheets("uno/dos?token=secreto", sesion=sesion).revision()
    assert sesion.peticiones[0][0].endswith("uno%2Fdos%3Ftoken%3Dsecreto")


@pytest.mark.parametrize("estado", [408, 429, 500, 502, 503, 504])
def test_http_transitorio_reintenta_y_cierra_cada_respuesta(documento: Objeto, estado: int) -> None:
    primera = Respuesta({}, estado)
    ultima = Respuesta(documento)
    sesion = Sesion(primera, ultima)
    assert lector(sesion).listar_hojas()
    assert len(sesion.peticiones) == 2
    assert primera.cierres == ultima.cierres == 1


@pytest.mark.parametrize(
    ("estado", "tipo"),
    [
        (400, EstructuraError),
        (401, AutenticacionError),
        (403, AutorizacionError),
        (404, AutorizacionError),
        (301, ServicioExternoError),
        (501, ServicioExternoError),
        (505, ServicioExternoError),
    ],
)
def test_http_permanente_no_se_reintenta(estado: int, tipo: type[Exception]) -> None:
    respuesta = Respuesta({"error": {"message": "secreto"}}, estado)
    sesion = Sesion(respuesta)
    with pytest.raises(tipo) as error:
        lector(sesion).revision()
    assert len(sesion.peticiones) == respuesta.cierres == 1
    assert "secreto" not in str(error.value)


@pytest.mark.parametrize("razon", ["rateLimitExceeded", "userRateLimitExceeded"])
def test_403_de_cuota_temporal_reintenta(documento: Objeto, razon: str) -> None:
    sesion = Sesion(Respuesta({"error": {"errors": [{"reason": razon}]}}, 403), documento)
    lector(sesion).revision()
    assert len(sesion.peticiones) == 2


@pytest.mark.parametrize(
    "contenido",
    [
        None,
        [],
        {"error": None},
        {"error": {"errors": None}},
        {"error": {"errors": []}},
        {"error": {"errors": [{"reason": []}]}},
        {"error": {"errors": [{"reason": "rateLimitExceeded"}, {"reason": "accessDenied"}]}},
        ValueError("secreto"),
    ],
)
def test_403_no_interpretable_o_mixto_no_reintenta(contenido: object) -> None:
    sesion = Sesion(Respuesta(contenido, 403))
    with pytest.raises(AutorizacionError):
        lector(sesion).revision()
    assert len(sesion.peticiones) == 1


@pytest.mark.parametrize(
    ("causa", "tipo"),
    [
        (Timeout("secreto"), TiempoEsperaError),
        (ConnectionError("secreto"), TransitorioError),
        (ChunkedEncodingError("secreto"), TransitorioError),
        (error_google(TransportError), TransitorioError),
    ],
)
def test_errores_de_red_reintentan_hasta_limite_y_conservan_causa(
    causa: Exception, tipo: type[Exception]
) -> None:
    sesion = Sesion(causa)
    with pytest.raises(tipo) as error:
        lector(sesion).revision()
    assert len(sesion.peticiones) == 4
    assert error.value.__cause__ is causa
    assert "secreto" not in str(error.value)


@pytest.mark.parametrize(
    ("causa", "tipo"),
    [
        (SSLError("secreto"), ServicioExternoError),
        (RequestException("secreto"), ServicioExternoError),
        (error_google(RefreshError), AutenticacionError),
        (error_google(GoogleAuthError), AutenticacionError),
    ],
)
def test_errores_permanentes_no_reintentan(causa: Exception, tipo: type[Exception]) -> None:
    sesion = Sesion(causa)
    with pytest.raises(tipo) as error:
        lector(sesion).revision()
    assert len(sesion.peticiones) == 1
    assert error.value.__cause__ is causa


def test_renovacion_temporal_se_reintenta(documento: Objeto) -> None:
    class RenovacionTemporalError(RefreshError):
        @property
        def retryable(self) -> bool:
            return True

    sesion = Sesion(error_google(RenovacionTemporalError), documento)
    lector(sesion).revision()
    assert len(sesion.peticiones) == 2


def test_json_invalido_cierra_respuesta_sin_reintentar() -> None:
    respuesta = Respuesta(ValueError("secreto"))
    sesion = Sesion(respuesta)
    with pytest.raises(EstructuraError):
        lector(sesion).revision()
    assert len(sesion.peticiones) == respuesta.cierres == 1


@pytest.mark.parametrize(
    "documento_invalido",
    [
        None,
        [],
        {1: "clave"},
        {"spreadsheetId": "otro", "sheets": []},
        {"spreadsheetId": "documento_sintetico", "sheets": {}},
        {"invalido": float("nan")},
        {"invalido": object()},
    ],
)
def test_respuesta_invalida_no_produce_instantanea(documento_invalido: object) -> None:
    with pytest.raises(EstructuraError):
        lector(Sesion(documento_invalido)).revision()


@pytest.mark.parametrize(
    "caso",
    [
        "falta_hoja",
        "nombre_duplicado",
        "id_duplicado",
        "id_bool",
        "tipo_hoja",
        "titulo",
        "dimension_bool",
        "fila_fuera",
        "columna_fuera",
        "bloque_fuera",
        "celda_duplicada",
    ],
)
def test_estructura_incompatible_se_rechaza(documento: Objeto, caso: str) -> None:
    hojas = cast(list[Objeto], documento["sheets"])
    destino = hoja(documento)
    propiedades = cast(Objeto, destino["properties"])
    if caso == "falta_hoja":
        hojas.pop()
    elif caso == "nombre_duplicado":
        hojas.append(deepcopy(destino))
    elif caso == "id_duplicado":
        propiedades["sheetId"] = 0
    elif caso == "id_bool":
        propiedades["sheetId"] = True
    elif caso == "tipo_hoja":
        propiedades["sheetType"] = "DATA_SOURCE"
    elif caso == "titulo":
        encontrar_celda(documento, 1, 1)["userEnteredValue"] = {"stringValue": "Incorrecto"}
    elif caso == "dimension_bool":
        cast(Objeto, propiedades["gridProperties"])["rowCount"] = True
    elif caso == "fila_fuera":
        cast(list[Objeto], destino["data"]).append({"startRow": 5004, "rowData": [{}, {}]})
    elif caso == "columna_fuera":
        celda(destino, 4, 801, texto("fuera"))
    elif caso == "bloque_fuera":
        cast(list[Objeto], destino["data"]).append({"startColumn": 800})
    else:
        celda(destino, 3, 1, texto("Duplicado"))
    with pytest.raises(EstructuraError):
        lector(Sesion(documento)).revision()


@pytest.mark.parametrize(
    "valor",
    [
        {"numberValue": 10},
        {"formulaValue": "=A1"},
        {"errorValue": {}},
        {"stringValue": 1},
        {"stringValue": "Nombre", "boolValue": True},
    ],
)
def test_nombres_deben_ser_texto_literal(documento: Objeto, valor: Objeto) -> None:
    encontrar_celda(documento, 3, 1)["userEnteredValue"] = valor
    with pytest.raises(EstructuraError):
        lector(Sesion(documento)).leer_clientes("IOL")


def test_nombres_derivados_y_errores_efectivos_se_rechazan(documento: Objeto) -> None:
    nombre = encontrar_celda(documento, 3, 1)
    nombre.pop("userEnteredValue")
    with pytest.raises(EstructuraError):
        lector(Sesion(documento)).revision()
    nombre["userEnteredValue"] = {"stringValue": "Axa"}
    nombre["effectiveValue"] = {"errorValue": {"type": "ERROR"}}
    with pytest.raises(EstructuraError):
        lector(Sesion(documento)).revision()


@pytest.mark.parametrize(
    ("valor", "formato", "esperado"),
    [
        ({"numberValue": 123}, "Fecha visible", "Fecha visible"),
        ({"numberValue": 123}, None, "123"),
        ({"boolValue": True}, None, "True"),
    ],
)
def test_encabezados_literales_conservan_valor_formateado(
    documento: Objeto, valor: Objeto, formato: str | None, esperado: str
) -> None:
    encabezado = encontrar_celda(documento, 2, 4)
    encabezado["userEnteredValue"] = valor
    if formato is None:
        encabezado.pop("formattedValue")
    else:
        encabezado["formattedValue"] = formato
    assert lector(Sesion(documento)).ultima_semana("IOL") == (4, esperado)


def test_encabezado_formula_o_formato_invalido_se_rechaza(documento: Objeto) -> None:
    encabezado = encontrar_celda(documento, 2, 4)
    encabezado["userEnteredValue"] = {"formulaValue": "=C2"}
    with pytest.raises(EstructuraError):
        lector(Sesion(documento)).revision()
    encabezado["userEnteredValue"] = {"numberValue": 123}
    encabezado["formattedValue"] = 123
    with pytest.raises(EstructuraError):
        lector(Sesion(documento)).revision()


def test_color_tema_se_resuelve_y_color_efectivo_prevalece(documento: Objeto) -> None:
    documento["properties"] = {
        "spreadsheetTheme": {
            "themeColors": [{"colorType": "ACCENT1", "color": {"rgbColor": VERDE}}]
        }
    }
    marca = encontrar_celda(documento, 3, 4)
    marca["effectiveFormat"] = {
        "backgroundColorStyle": {"themeColor": "ACCENT1"},
        "backgroundColor": {},
    }
    assert lector(Sesion(documento)).leer_marcas("IOL", 4) == frozenset({3, 3005})
    marca["effectiveFormat"] = {"backgroundColorStyle": {"rgbColor": {}}, "backgroundColor": VERDE}
    marca["userEnteredFormat"] = {"backgroundColorStyle": {"rgbColor": VERDE}}
    assert lector(Sesion(documento)).leer_marcas("IOL", 4) == frozenset({3005})


def test_tema_no_resuelto_impide_solo_leer_marcas_y_x_tiene_prioridad(documento: Objeto) -> None:
    marca = encontrar_celda(documento, 3, 4)
    marca["effectiveFormat"] = {"backgroundColorStyle": {"themeColor": "ACCENT1"}}
    lectura = lector(Sesion(documento))
    assert lectura.leer_clientes("IOL")
    with pytest.raises(EstructuraError):
        lectura.leer_marcas("IOL", 4)
    marca["userEnteredValue"] = {"stringValue": "x"}
    assert lector(Sesion(documento)).leer_marcas("IOL", 4) == frozenset({3, 3005})


def test_formato_default_y_ausencia_de_efectivo_no_usan_formato_ingresado(
    documento: Objeto,
) -> None:
    marca = encontrar_celda(documento, 3, 4)
    marca.pop("effectiveFormat")
    marca["userEnteredFormat"] = {"backgroundColorStyle": {"rgbColor": VERDE}}
    assert lector(Sesion(documento)).leer_marcas("IOL", 4) == frozenset({3005})
    documento["properties"] = {"defaultFormat": {"backgroundColorStyle": {"rgbColor": VERDE}}}
    assert lector(Sesion(documento)).leer_marcas("IOL", 4) == frozenset({3, 3005})


@pytest.mark.parametrize("componente", [True, -0.1, 1.1, "verde", 10**400])
def test_colores_invalidos_no_se_interpretan_como_ausencia_de_marca(
    documento: Objeto, componente: object
) -> None:
    encontrar_celda(documento, 3, 4)["effectiveFormat"] = {"backgroundColor": {"red": componente}}
    with pytest.raises(EstructuraError):
        lector(Sesion(documento)).revision()


def test_fabrica_configura_timeout_reintentos_y_cierra_ambas_sesiones(
    tmp_path: Path,
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
    documento: Objeto,
) -> None:
    for nombre in tuple(environ):
        if nombre.startswith("AC_"):
            monkeypatch.delenv(nombre)
    configuracion = Config(
        directorio_base=tmp_path, spreadsheet_id="documento_sintetico", _env_file=None
    )
    credenciales = cast(Credentials, mocker.Mock(spec=Credentials))
    construir = mocker.patch.object(modulo, "AuthorizedSession", autospec=True)
    renovacion = mocker.patch.object(modulo, "Session", autospec=True)
    lectura = crear_lector_sheets(configuracion, credenciales)
    assert construir.call_args.kwargs["max_refresh_attempts"] == 0
    assert construir.call_args.kwargs["refresh_timeout"] == configuracion.timeout_red_segundos
    construir.return_value.get.assert_not_called()
    renovacion.return_value.get.assert_not_called()
    construir.return_value.get.return_value = Respuesta(documento)
    lectura.revision()
    assert (
        construir.return_value.get.call_args.kwargs["timeout"] == configuracion.timeout_red_segundos
    )
    lectura.cerrar()
    lectura.cerrar()
    construir.return_value.close.assert_called_once()
    renovacion.return_value.close.assert_called_once()


def test_fabrica_fallida_libera_sesion_de_renovacion(tmp_path: Path, mocker: MockerFixture) -> None:
    configuracion = Config(directorio_base=tmp_path, spreadsheet_id="documento", _env_file=None)
    renovacion = mocker.patch.object(modulo, "Session", autospec=True)
    mocker.patch.object(modulo, "AuthorizedSession", side_effect=RuntimeError("Fallo simulado."))
    with pytest.raises(RuntimeError):
        crear_lector_sheets(configuracion, cast(Credentials, mocker.Mock(spec=Credentials)))
    renovacion.return_value.close.assert_called_once()
    with pytest.raises(ConfiguracionError):
        crear_lector_sheets(
            configuracion.model_copy(update={"spreadsheet_id": None}),
            cast(Credentials, mocker.Mock()),
        )


def test_fabrica_real_usa_credenciales_ficticias_y_http_simulado(
    tmp_path: Path,
    documento: Objeto,
    mocker: MockerFixture,
) -> None:
    fabricar = cast(Callable[[], Credentials], AnonymousCredentials)
    credenciales = fabricar()
    configuracion = Config(
        directorio_base=tmp_path, spreadsheet_id="documento_sintetico", _env_file=None
    )
    consulta = mocker.patch.object(Session, "request", return_value=Respuesta(documento))
    with crear_lector_sheets(configuracion, credenciales) as lectura:
        assert lectura.leer_clientes("IOL")[0].nombre_crudo == "  Axa Prueba  "
    consulta.assert_called_once()
    assert consulta.call_args.kwargs["timeout"] == configuracion.timeout_red_segundos
    assert consulta.call_args.kwargs["allow_redirects"] is False


def test_logs_no_exponen_identificador_contactos_ni_error_tecnico(
    documento: Objeto, caplog: pytest.LogCaptureFixture
) -> None:
    sesion = Sesion(ConnectionError("detalle secreto"), documento)
    with caplog.at_level("DEBUG"):
        lector(sesion).revision()
    assert "reintentará" in caplog.text
    assert "Instantánea Sheets validada" in caplog.text
    assert "documento_sintetico" not in caplog.text
    assert "Axa Prueba" not in caplog.text
    assert "detalle secreto" not in caplog.text


@pytest.mark.parametrize(
    "valor",
    [
        {"numberValue": True},
        {"numberValue": "123"},
        {"numberValue": 10**400},
        {"boolValue": "true"},
    ],
)
def test_encabezados_numericos_y_booleanos_deben_tener_tipo_correcto(
    documento: Objeto, valor: Objeto
) -> None:
    encontrar_celda(documento, 2, 4)["userEnteredValue"] = valor
    with pytest.raises(EstructuraError):
        lector(Sesion(documento)).revision()


@pytest.mark.parametrize("caso", ["tema_duplicado", "tema_sin_rgb", "tipo_tema", "estilo_doble"])
def test_temas_y_estilos_ambiguos_se_rechazan(documento: Objeto, caso: str) -> None:
    pareja: Objeto = {"colorType": "ACCENT1", "color": {"rgbColor": VERDE}}
    if caso == "tema_duplicado":
        colores = [pareja, pareja]
    elif caso == "tema_sin_rgb":
        pareja["color"] = {"themeColor": "ACCENT2"}
        colores = [pareja]
    elif caso == "tipo_tema":
        pareja["colorType"] = 12
        colores = [pareja]
    else:
        colores = [pareja]
        encontrar_celda(documento, 3, 4)["effectiveFormat"] = {
            "backgroundColorStyle": {"rgbColor": VERDE, "themeColor": "ACCENT1"}
        }
    documento["properties"] = {"spreadsheetTheme": {"themeColors": colores}}
    with pytest.raises(EstructuraError):
        lector(Sesion(documento)).revision()


def test_fallo_de_cierre_no_oculta_error_del_bloque(
    documento: Objeto, mocker: MockerFixture
) -> None:
    sesion = Sesion(documento)
    mocker.patch.object(sesion, "close", side_effect=OSError("secreto"))
    lectura = LectorClientesSheets("documento_sintetico", sesion=sesion, cerrar_sesion=True)
    with pytest.raises(ValueError, match="original"), lectura:
        raise ValueError("original")
    otra = LectorClientesSheets("documento_sintetico", sesion=sesion, cerrar_sesion=True)
    with pytest.raises(ServicioExternoError), otra:
        pass


def test_bloques_contiguos_usan_desplazamientos_y_omiten_indices_cero(documento: Objeto) -> None:
    destino = hoja(documento, "PPI")
    destino["data"] = [
        {
            "rowData": [
                {"values": [texto("Cliente")]},
                {"values": [{}, {}, texto("Semana")]},
                {"values": [texto("Otra Persona"), {}, texto("x")]},
            ]
        }
    ]
    lectura = lector(Sesion(documento))
    assert lectura.leer_clientes("PPI")[0].fila == 3
    assert lectura.leer_marcas("PPI", 3) == frozenset({3})
