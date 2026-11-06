"""Lotes, respaldos y registro con Sheets simulado y archivos temporales."""

import json
from collections.abc import Callable, Mapping
from copy import deepcopy
from datetime import UTC, date, datetime
from os import environ
from pathlib import Path
from typing import cast

import pytest
from google.auth.credentials import AnonymousCredentials
from google.auth.exceptions import GoogleAuthError, RefreshError
from google.oauth2.credentials import Credentials
from pytest_mock import MockerFixture
from requests import Response, Session
from requests.exceptions import ConnectionError, RequestException, SSLError, Timeout

from automatizacion_clientes.config import Config
from automatizacion_clientes.core.catalogo import construir_catalogo
from automatizacion_clientes.core.contratos import EscritorClientes
from automatizacion_clientes.core.errores import (
    AutenticacionError,
    AutorizacionError,
    ConcurrenciaError,
    ConfiguracionError,
    EstructuraError,
    PersistenciaError,
    RegistroInterrumpidoError,
    ServicioExternoError,
    TiempoEsperaError,
    TransitorioError,
)
from automatizacion_clientes.core.modelos import (
    PeriodoSemanal,
    ResolucionSemanal,
    SemanaContactos,
)
from automatizacion_clientes.infra import escritor_clientes_sheets as modulo
from automatizacion_clientes.infra.escritor_clientes_sheets import (
    EscritorClientesSheets,
    crear_escritor_sheets,
)
from automatizacion_clientes.infra.lector_clientes_sheets import LectorClientesSheets
from automatizacion_clientes.services.registro import ServicioRegistro

type Objeto = dict[str, object]


class RelojFijo:
    def ahora(self) -> datetime:
        return datetime(2026, 11, 6, 12, tzinfo=UTC)


class Respuesta(Response):
    def __init__(self, contenido: object, estado: int = 200) -> None:
        super().__init__()
        self.status_code = estado
        self.contenido = contenido
        self.cierres = 0

    def json(self, **kwargs: object) -> object:
        if isinstance(self.contenido, Exception):
            raise self.contenido
        return deepcopy(self.contenido)

    def close(self) -> None:
        self.cierres += 1


def objeto(valor: object) -> Objeto:
    return cast(Objeto, valor)


def lista(valor: object) -> list[Objeto]:
    return cast(list[Objeto], valor)


def hoja(documento: Objeto, nombre: str = "IOL") -> Objeto:
    return next(
        destino
        for destino in lista(documento["sheets"])
        if objeto(destino["properties"])["title"] == nombre
    )


def celdas(destino: Objeto) -> list[Objeto]:
    return lista(lista(destino["data"])[0]["rowData"])


def celda(destino: Objeto, fila: int, columna: int) -> Objeto:
    filas = celdas(destino)
    while len(filas) < fila:
        filas.append({"values": []})
    valores = lista(filas[fila - 1]["values"])
    while len(valores) < columna:
        valores.append({})
    return valores[columna - 1]


class SesionSimulada:
    def __init__(self, documento: Objeto) -> None:
        self.documento = documento
        self.envios: list[Objeto] = []
        self.consultas = 0
        self.cierres = 0
        self.resultados: list[Response | Exception] = []
        self.antes_get: Callable[[], None] | None = None
        self.despues_post: Callable[[], None] | None = None
        self.fallo_get: Exception | None = None
        self.ultima_respuesta: Respuesta | None = None

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str | list[str]],
        timeout: float,
        allow_redirects: bool,
    ) -> Response:
        self.consultas += 1
        assert timeout > 0
        assert allow_redirects is False
        if self.fallo_get is not None:
            raise self.fallo_get
        if self.antes_get is not None:
            self.antes_get()
        return Respuesta(self.documento)

    def post(
        self,
        url: str,
        *,
        json: Mapping[str, object],
        timeout: float,
        allow_redirects: bool,
    ) -> Response:
        assert url.endswith("documento_sintetico:batchUpdate")
        assert timeout > 0
        assert allow_redirects is False
        self.envios.append(dict(deepcopy(json)))
        if self.resultados:
            resultado = self.resultados.pop(0)
            if isinstance(resultado, Exception):
                raise resultado
            return resultado
        for peticion in lista(json["requests"]):
            if "updateSheetProperties" in peticion:
                propiedades = objeto(objeto(peticion["updateSheetProperties"])["properties"])
                destino = self._por_id(propiedades["sheetId"])
                objeto(objeto(destino["properties"])["gridProperties"]).update(
                    objeto(propiedades["gridProperties"])
                )
                continue
            actualizacion = objeto(peticion["updateCells"])
            inicio = objeto(actualizacion["start"])
            destino = self._por_id(inicio["sheetId"])
            for desplazamiento, fila in enumerate(lista(actualizacion["rows"]), 1):
                contenido = lista(fila["values"])[0]
                actual = celda(
                    destino,
                    cast(int, inicio["rowIndex"]) + desplazamiento,
                    cast(int, inicio["columnIndex"]) + 1,
                )
                actual.pop("userEnteredValue", None)
                actual.pop("effectiveValue", None)
                if "userEnteredValue" in contenido:
                    actual["userEnteredValue"] = deepcopy(contenido["userEnteredValue"])
                    actual["effectiveValue"] = deepcopy(contenido["userEnteredValue"])
                if "userEnteredFormat" in contenido:
                    formato = objeto(actual.setdefault("userEnteredFormat", {}))
                    formato.pop("backgroundColor", None)
                    formato.update(deepcopy(objeto(contenido["userEnteredFormat"])))
                    actual["effectiveFormat"] = deepcopy(formato)
        if self.despues_post is not None:
            self.despues_post()
        self.ultima_respuesta = Respuesta({"spreadsheetId": "documento_sintetico"})
        return self.ultima_respuesta

    def _por_id(self, identificador: object) -> Objeto:
        return next(
            destino
            for destino in lista(self.documento["sheets"])
            if objeto(destino["properties"])["sheetId"] == identificador
        )

    def close(self) -> None:
        self.cierres += 1


@pytest.fixture(autouse=True)
def aislar_entorno(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for nombre in tuple(environ):
        if nombre.upper().startswith("AC_"):
            monkeypatch.delenv(nombre)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def documento() -> Objeto:
    resultado: Objeto = {"spreadsheetId": "documento_sintetico", "sheets": []}
    for indice, nombre in enumerate(("Inviu", "IOL", "Bull", "Balanz", "PPI")):
        destino: Objeto = {
            "properties": {
                "sheetId": indice * 10,
                "title": nombre,
                "gridProperties": {"rowCount": 100, "columnCount": 4},
            },
            "data": [{"rowData": []}],
        }
        celda(destino, 1, 1)["userEnteredValue"] = {"stringValue": "Cliente"}
        celda(destino, 2, 4 if nombre == "Balanz" else 3)["userEnteredValue"] = {
            "stringValue": "Semana"
        }
        celda(destino, 3, 1)["userEnteredValue"] = {"stringValue": "Axa Prueba"}
        celda(destino, 5, 1)["userEnteredValue"] = {"stringValue": "Notuka Ejemplo"}
        celda(destino, 3, 2)["userEnteredValue"] = {"formulaValue": "=1+2"}
        lista(resultado["sheets"]).append(destino)
    destino = hoja(resultado)
    celda(destino, 5, 3)["userEnteredValue"] = {"stringValue": " X "}
    celda(destino, 3, 3)["userEnteredFormat"] = {"textFormat": {"bold": True}}
    celda(destino, 4, 3)["note"] = "Hueco que no debe escribirse"
    return resultado


def lector(sesion: SesionSimulada) -> LectorClientesSheets:
    return LectorClientesSheets(
        "documento_sintetico",
        sesion=sesion,
        intentos=3,
        espera_minima_segundos=0.001,
        espera_maxima_segundos=0.002,
    )


def escritor(sesion: SesionSimulada, tmp_path: Path) -> EscritorClientesSheets:
    return EscritorClientesSheets(
        "documento_sintetico",
        sesion=sesion,
        directorio_respaldos=tmp_path / "respaldos",
        reloj=RelojFijo(),
        intentos=3,
        espera_minima_segundos=0.001,
        espera_maxima_segundos=0.002,
    )


def aplicar(destino: EscritorClientesSheets, revision: str) -> None:
    destino.aplicar_marcas("IOL", 3, "Semana", frozenset({3}), revision_esperada=revision)


def test_constructor_no_hace_io_y_cumple_contrato(documento: Objeto, tmp_path: Path) -> None:
    sesion = SesionSimulada(documento)
    destino: EscritorClientes = escritor(sesion, tmp_path)
    assert destino is not None
    assert sesion.consultas == 0
    assert not list(tmp_path.iterdir())


def test_reemplaza_solo_clientes_y_conserva_huecos_formulas_y_estilos(
    documento: Objeto,
    tmp_path: Path,
) -> None:
    sesion = SesionSimulada(documento)
    lectura = lector(sesion)
    revision = lectura.revision()
    original = deepcopy(documento)
    aplicar(escritor(sesion, tmp_path), revision)
    assert sesion.ultima_respuesta is not None
    assert sesion.ultima_respuesta.cierres == 1
    assert len(sesion.envios) == 1
    assert lectura.revision() != revision
    assert lectura.leer_marcas("IOL", 3) == frozenset({3})
    for nombre in ("Inviu", "Bull", "Balanz", "PPI"):
        assert hoja(documento, nombre) == hoja(original, nombre)
    destino = hoja(documento)
    assert celda(destino, 4, 3) == celda(hoja(original), 4, 3)
    assert celda(destino, 3, 2) == celda(hoja(original), 3, 2)
    assert objeto(celda(destino, 3, 3)["userEnteredFormat"])["textFormat"] == {"bold": True}
    assert "userEnteredValue" not in celda(destino, 5, 3)
    assert not (tmp_path / "respaldos").exists()  # el servicio decide respaldar


@pytest.mark.parametrize(("nombre", "columna"), [("IOL", 4), ("Balanz", 5)])
def test_crea_semana_literal_y_amplia_solo_si_falta_espacio(
    documento: Objeto,
    tmp_path: Path,
    nombre: str,
    columna: int,
) -> None:
    sesion = SesionSimulada(documento)
    revision = lector(sesion).revision()
    escritor(sesion, tmp_path).aplicar_marcas(
        nombre, columna, " Nueva ", frozenset({3}), revision_esperada=revision
    )
    lectura = lector(sesion)
    assert lectura.ultima_semana(nombre) == (columna, "Nueva")
    assert lectura.leer_marcas(nombre, columna) == frozenset({3})
    assert objeto(objeto(hoja(documento, nombre)["properties"])["gridProperties"])[
        "columnCount"
    ] == max(4, columna)
    assert all(
        "appendDimension" not in peticion for peticion in lista(sesion.envios[0]["requests"])
    )


def test_respaldo_exclusivo_contiene_instantanea_y_no_escribe_remoto(
    documento: Objeto,
    tmp_path: Path,
) -> None:
    sesion = SesionSimulada(documento)
    destino = escritor(sesion, tmp_path)
    primero = destino.crear_respaldo()
    segundo = destino.crear_respaldo()
    assert primero != segundo
    contenido = objeto(json.loads(primero.read_text(encoding="utf-8")))
    assert contenido["version"] == 1
    assert contenido["documento"] == documento
    assert contenido["revision"] == lector(sesion).revision()
    assert not sesion.envios


def test_cambio_durante_respaldo_elimina_copia(documento: Objeto, tmp_path: Path) -> None:
    sesion = SesionSimulada(documento)

    def cambiar() -> None:
        if sesion.consultas == 2:
            celda(hoja(documento), 3, 1)["userEnteredValue"] = {"stringValue": "Otra Persona"}

    sesion.antes_get = cambiar
    with pytest.raises(ConcurrenciaError):
        escritor(sesion, tmp_path).crear_respaldo()
    assert not list((tmp_path / "respaldos").iterdir())
    assert not sesion.envios


def test_error_de_disco_no_escribe_remoto(
    documento: Objeto,
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    sesion = SesionSimulada(documento)
    mocker.patch.object(modulo, "fsync", side_effect=OSError("ficticio"))
    with pytest.raises(PersistenciaError):
        escritor(sesion, tmp_path).crear_respaldo()
    assert not list((tmp_path / "respaldos").iterdir())
    assert not sesion.envios


def test_revision_obsoleta_no_envia_lote(documento: Objeto, tmp_path: Path) -> None:
    sesion = SesionSimulada(documento)
    with pytest.raises(ConcurrenciaError):
        aplicar(escritor(sesion, tmp_path), "obsoleta")
    assert not sesion.envios


@pytest.mark.parametrize("estado", [408, 429, 500, 502, 503, 504])
def test_reintento_http_comprueba_revision_antes_de_reenviar(
    documento: Objeto,
    tmp_path: Path,
    estado: int,
) -> None:
    sesion = SesionSimulada(documento)
    revision = lector(sesion).revision()
    sesion.resultados = [Respuesta({}, estado)]
    aplicar(escritor(sesion, tmp_path), revision)
    assert len(sesion.envios) == 2
    assert sesion.consultas == 5
    assert sesion.envios[0] == sesion.envios[1]


@pytest.mark.parametrize("causa", [Timeout("ficticio"), ConnectionError("ficticio")])
def test_reintentos_de_conexion_son_acotados(
    documento: Objeto,
    tmp_path: Path,
    causa: Exception,
) -> None:
    sesion = SesionSimulada(documento)
    revision = lector(sesion).revision()
    sesion.resultados = [causa, causa, causa]
    with pytest.raises(TransitorioError):
        aplicar(escritor(sesion, tmp_path), revision)
    assert len(sesion.envios) == 3
    assert sesion.consultas == 7


def test_timeout_tras_aplicar_detiene_reenvio_por_revision(
    documento: Objeto,
    tmp_path: Path,
) -> None:
    sesion = SesionSimulada(documento)
    revision = lector(sesion).revision()

    def perder_confirmacion() -> None:
        raise Timeout("se perdió la respuesta ficticia")

    sesion.despues_post = perder_confirmacion
    with pytest.raises(ConcurrenciaError):
        aplicar(escritor(sesion, tmp_path), revision)
    assert len(sesion.envios) == 1
    assert lector(sesion).leer_marcas("IOL", 3) == frozenset({3})


@pytest.mark.parametrize(
    ("estado", "categoria"),
    [
        (400, EstructuraError),
        (401, AutenticacionError),
        (403, AutorizacionError),
        (404, AutorizacionError),
        (501, ServicioExternoError),
        (505, ServicioExternoError),
    ],
)
def test_error_permanente_no_reintenta_y_cierra_respuesta(
    documento: Objeto,
    tmp_path: Path,
    estado: int,
    categoria: type[Exception],
) -> None:
    sesion = SesionSimulada(documento)
    respuesta = Respuesta({"error": "secreto ficticio"}, estado)
    sesion.resultados = [respuesta]
    with pytest.raises(categoria) as error:
        aplicar(escritor(sesion, tmp_path), lector(sesion).revision())
    assert len(sesion.envios) == 1
    assert respuesta.cierres == 1
    assert "secreto ficticio" not in str(error.value)


def test_tls_no_reintenta(documento: Objeto, tmp_path: Path) -> None:
    sesion = SesionSimulada(documento)
    sesion.resultados = [SSLError("ficticio")]
    with pytest.raises(ServicioExternoError):
        aplicar(escritor(sesion, tmp_path), lector(sesion).revision())
    assert len(sesion.envios) == 1


def test_fallo_de_lectura_no_multiplica_reintentos(documento: Objeto, tmp_path: Path) -> None:
    sesion = SesionSimulada(documento)
    revision = lector(sesion).revision()
    sesion.fallo_get = Timeout("ficticio")
    with pytest.raises(TiempoEsperaError):
        aplicar(escritor(sesion, tmp_path), revision)
    assert sesion.consultas == 4
    assert not sesion.envios


@pytest.mark.parametrize("contenido", [ValueError("ficticio"), {}, [], {"spreadsheetId": "otro"}])
def test_confirmacion_invalida_no_repite_post(
    documento: Objeto,
    tmp_path: Path,
    contenido: object,
) -> None:
    sesion = SesionSimulada(documento)
    sesion.resultados = [Respuesta(contenido)]
    with pytest.raises(EstructuraError):
        aplicar(escritor(sesion, tmp_path), lector(sesion).revision())
    assert len(sesion.envios) == 1


@pytest.mark.parametrize(
    "campo", ["merges", "protectedRanges", "bandedRanges", "conditionalFormats"]
)
def test_rechaza_restricciones_superpuestas_pero_admite_otra_columna(
    documento: Objeto,
    tmp_path: Path,
    campo: str,
) -> None:
    destino = hoja(documento)
    rango = {"sheetId": 10, "startColumnIndex": 2, "endColumnIndex": 3}
    destino[campo] = [
        rango
        if campo == "merges"
        else {"ranges": [rango]}
        if campo == "conditionalFormats"
        else {"range": rango}
    ]
    sesion = SesionSimulada(documento)
    with pytest.raises(EstructuraError):
        aplicar(escritor(sesion, tmp_path), lector(sesion).revision())
    assert not sesion.envios
    rango["startColumnIndex"] = 1
    rango["endColumnIndex"] = 2
    aplicar(escritor(sesion, tmp_path), lector(sesion).revision())
    assert len(sesion.envios) == 1


@pytest.mark.parametrize(
    "valor",
    [
        {"formulaValue": "=1"},
        {"numberValue": 3},
        {"stringValue": "otro"},
        {"errorValue": {"type": "REF"}},
    ],
)
@pytest.mark.parametrize("campo", ["userEnteredValue", "effectiveValue"])
def test_rechaza_datos_ajenos_en_semana(
    documento: Objeto,
    tmp_path: Path,
    valor: Objeto,
    campo: str,
) -> None:
    celda(hoja(documento), 3, 3)[campo] = valor
    sesion = SesionSimulada(documento)
    with pytest.raises(EstructuraError):
        aplicar(escritor(sesion, tmp_path), lector(sesion).revision())
    assert not sesion.envios


@pytest.mark.parametrize("caso", ["tables", "pivotTable", "dataSourceFormula", "dataValidation"])
def test_rechaza_tablas_pivotes_y_validaciones(
    documento: Objeto,
    tmp_path: Path,
    caso: str,
) -> None:
    if caso == "tables":
        hoja(documento)[caso] = [{"tableId": "ficticio"}]
    else:
        celda(hoja(documento), 3, 3)[caso] = {"ficticio": True}
    sesion = SesionSimulada(documento)
    with pytest.raises(EstructuraError):
        aplicar(escritor(sesion, tmp_path), lector(sesion).revision())
    assert not sesion.envios


@pytest.mark.parametrize(
    ("nombre", "columna", "encabezado", "filas"),
    [
        ("Otra", 3, "Semana", frozenset({3})),
        ("IOL", 2, "Semana", frozenset({3})),
        ("IOL", 5, "Nueva", frozenset({3})),
        ("IOL", 3, "=SUM(A1)", frozenset({3})),
        ("IOL", 3, "", frozenset({3})),
        ("IOL", 3, "Semana", frozenset({4})),
        ("IOL", 3, "Semana", frozenset({True})),
    ],
)
def test_peticion_incompatible_no_escribe(
    documento: Objeto,
    tmp_path: Path,
    nombre: str,
    columna: int,
    encabezado: str,
    filas: frozenset[int],
) -> None:
    sesion = SesionSimulada(documento)
    with pytest.raises(EstructuraError):
        escritor(sesion, tmp_path).aplicar_marcas(
            nombre, columna, encabezado, filas, revision_esperada=lector(sesion).revision()
        )
    assert not sesion.envios


def test_cierre_respeta_propiedad_e_invalida(documento: Objeto, tmp_path: Path) -> None:
    sesion = SesionSimulada(documento)
    with escritor(sesion, tmp_path) as destino:
        pass
    assert sesion.cierres == 0
    destino.cerrar()
    with pytest.raises(ConfiguracionError):
        destino.crear_respaldo()
    with pytest.raises(ConfiguracionError):
        aplicar(destino, "revision")


def test_fabrica_rechaza_solo_lectura_sin_crear_sesion(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    fabricar = mocker.patch.object(modulo, "AuthorizedSession")
    configuracion = Config(
        directorio_base=tmp_path, spreadsheet_id="documento_sintetico", _env_file=None
    )
    with pytest.raises(ConfiguracionError):
        crear_escritor_sheets(
            configuracion,
            cast(Callable[[], AnonymousCredentials], AnonymousCredentials)(),
            reloj=RelojFijo(),
        )
    fabricar.assert_not_called()


def test_fabrica_configura_y_cierra_ambas_sesiones(tmp_path: Path, mocker: MockerFixture) -> None:
    alcance = "https://www.googleapis.com/auth/spreadsheets"
    configuracion = Config(
        directorio_base=tmp_path,
        spreadsheet_id="documento_sintetico",
        google_alcances=(alcance,),
        _env_file=None,
    )
    fabricar_credencial = cast(Callable[..., Credentials], Credentials)
    credencial = fabricar_credencial(token="ficticio", scopes=[alcance])
    autorizada = mocker.Mock(spec=Session)
    renovacion = mocker.Mock(spec=Session)
    fabricar = mocker.patch.object(modulo, "AuthorizedSession", return_value=autorizada)
    mocker.patch.object(modulo, "Session", return_value=renovacion)
    with crear_escritor_sheets(configuracion, credencial, reloj=RelojFijo()) as destino:
        assert destino is not None
    assert fabricar.call_args.kwargs["max_refresh_attempts"] == 0
    assert fabricar.call_args.kwargs["refresh_timeout"] == configuracion.timeout_red_segundos
    autorizada.close.assert_called_once()
    renovacion.close.assert_called_once()


@pytest.mark.parametrize("fallar", [False, True])
def test_servicio_registro_simula_respalda_y_detecta_resultado_incierto(
    documento: Objeto,
    tmp_path: Path,
    fallar: bool,
) -> None:
    sesion = SesionSimulada(documento)
    lectura = lector(sesion)
    revision = lectura.revision()
    catalogo = construir_catalogo(
        identificador_origen="sheets_sintetico",
        revision=revision,
        generado_en=RelojFijo().ahora(),
        hojas=lectura.listar_hojas(),
        clientes_por_hoja={
            info.nombre: lectura.leer_clientes(info.nombre) for info in lectura.listar_hojas()
        },
    )
    semana = SemanaContactos(
        periodo=PeriodoSemanal(desde=date(2026, 11, 2), hasta=date(2026, 11, 6)),
        contactos=(),
    )
    servicio = ServicioRegistro(
        lector_clientes=lectura,
        escritor_clientes=escritor(sesion, tmp_path),
        identificador_origen="sheets_sintetico",
    )
    plan = servicio.planificar(
        catalogo=catalogo, semana=semana, resolucion=ResolucionSemanal(), encabezado="Nueva"
    )
    assert servicio.ejecutar(plan).simulacion
    assert not sesion.envios
    assert not (tmp_path / "respaldos").exists()
    original = deepcopy(documento)
    respaldo: Path | str | None
    if fallar:

        def perder_confirmacion() -> None:
            raise Timeout("ficticio")

        sesion.despues_post = perder_confirmacion
        with pytest.raises(RegistroInterrumpidoError) as error:
            servicio.ejecutar(plan, simulacion=False)
        assert error.value.hojas_confirmadas == ()
        assert error.value.hoja_en_curso == "Inviu"
        assert len(sesion.envios) == 1
        respaldo = error.value.respaldo
    else:
        resultado = servicio.ejecutar(plan, simulacion=False)
        assert resultado.hojas_aplicadas == ("Inviu", "IOL", "Bull", "Balanz", "PPI")
        assert all(
            lectura.ultima_semana(info.nombre)[1] == "Nueva" for info in lectura.listar_hojas()
        )
        respaldo = resultado.respaldo
    assert isinstance(respaldo, Path)
    assert objeto(json.loads(respaldo.read_text(encoding="utf-8")))["documento"] == original


def test_cambio_entre_preparacion_y_envio_no_escribe(documento: Objeto, tmp_path: Path) -> None:
    sesion = SesionSimulada(documento)
    revision = lector(sesion).revision()

    def cambiar() -> None:
        if sesion.consultas == 3:
            celda(hoja(documento), 5, 1)["userEnteredValue"] = {"stringValue": "Otra Persona"}

    sesion.antes_get = cambiar
    with pytest.raises(ConcurrenciaError):
        aplicar(escritor(sesion, tmp_path), revision)
    assert not sesion.envios


def test_repetir_semana_nueva_no_duplica_columnas(documento: Objeto, tmp_path: Path) -> None:
    sesion = SesionSimulada(documento)
    destino = escritor(sesion, tmp_path)
    for _ in range(2):
        destino.aplicar_marcas(
            "Balanz", 5, "Nueva", frozenset({3}), revision_esperada=lector(sesion).revision()
        )
    lectura = lector(sesion)
    assert lectura.leer_encabezados("Balanz") == ((4, "Semana"), (5, "Nueva"))
    assert lectura.leer_marcas("Balanz", 5) == frozenset({3})
    assert (
        objeto(objeto(hoja(documento, "Balanz")["properties"])["gridProperties"])["columnCount"]
        == 5
    )


def test_hoja_vacia_sin_semanas_crea_primera_en_d_para_balanz(
    documento: Objeto,
    tmp_path: Path,
) -> None:
    destino = hoja(documento, "Balanz")
    destino["data"] = [
        {"rowData": [{"values": [{"userEnteredValue": {"stringValue": "Cliente"}}]}]}
    ]
    sesion = SesionSimulada(documento)
    escritor(sesion, tmp_path).aplicar_marcas(
        "Balanz", 4, "Primera", frozenset(), revision_esperada=lector(sesion).revision()
    )
    assert lector(sesion).ultima_semana("Balanz") == (4, "Primera")
    assert len(lista(sesion.envios[0]["requests"])) == 1


def test_instantanea_publica_no_comparte_estado_mutable(documento: Objeto) -> None:
    sesion = SesionSimulada(documento)
    lectura = lector(sesion)
    revision, instantanea = lectura.leer_instantanea()
    objeto(hoja(instantanea)["properties"])["title"] = "Alterada"
    celda(hoja(instantanea, "Inviu"), 3, 1)["userEnteredValue"] = {"stringValue": "Alterado"}
    assert lectura.listar_hojas()[1].nombre == "IOL"
    assert lectura.leer_clientes("Inviu")[0].nombre_crudo == "Axa Prueba"
    assert lectura.revision() == revision


@pytest.mark.parametrize(
    "causa",
    [
        cast(Callable[[str], GoogleAuthError], GoogleAuthError)("ficticio"),
        cast(Callable[[str], RefreshError], RefreshError)("ficticio"),
        RequestException("ficticio"),
    ],
)
def test_errores_de_autorizacion_o_peticion_no_reintentan(
    documento: Objeto,
    tmp_path: Path,
    causa: Exception,
) -> None:
    sesion = SesionSimulada(documento)
    sesion.resultados = [causa]
    with pytest.raises((AutenticacionError, ServicioExternoError)):
        aplicar(escritor(sesion, tmp_path), lector(sesion).revision())
    assert len(sesion.envios) == 1


@pytest.mark.parametrize("caso", ["sin_id", "sin_permiso", "falla_constructor"])
def test_fabrica_rechaza_configuracion_o_credenciales_y_libera_recursos(
    tmp_path: Path,
    mocker: MockerFixture,
    caso: str,
) -> None:
    alcance = "https://www.googleapis.com/auth/spreadsheets"
    configuracion = Config(
        directorio_base=tmp_path,
        spreadsheet_id=None if caso == "sin_id" else "documento_sintetico",
        google_alcances=(alcance,),
        _env_file=None,
    )
    fabricar_credencial = cast(Callable[..., Credentials], Credentials)
    credencial = fabricar_credencial(
        token="ficticio", scopes=[] if caso == "sin_permiso" else [alcance]
    )
    renovacion = mocker.Mock(spec=Session)
    mocker.patch.object(modulo, "Session", return_value=renovacion)
    fabricar = mocker.patch.object(
        modulo, "AuthorizedSession", side_effect=RuntimeError("ficticio")
    )
    categoria = {
        "sin_id": ConfiguracionError,
        "sin_permiso": AutorizacionError,
        "falla_constructor": RuntimeError,
    }[caso]
    with pytest.raises(categoria):
        crear_escritor_sheets(configuracion, credencial, reloj=RelojFijo())
    if caso == "falla_constructor":
        renovacion.close.assert_called_once()
    else:
        fabricar.assert_not_called()


def test_cierre_fallido_conserva_excepcion_del_bloque(
    documento: Objeto,
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    sesion = SesionSimulada(documento)
    destino = EscritorClientesSheets(
        "documento_sintetico",
        sesion=sesion,
        directorio_respaldos=tmp_path,
        reloj=RelojFijo(),
        cerrar_sesion=True,
    )
    mocker.patch.object(sesion, "close", side_effect=OSError("ficticio"))
    with pytest.raises(RuntimeError, match="original"), destino:
        raise RuntimeError("original")
    destino.cerrar()


@pytest.mark.parametrize(
    "rango",
    [
        {"sheetId": 99},
        {"sheetId": 10, "startColumnIndex": -1},
        {"sheetId": 10, "startColumnIndex": 3, "endColumnIndex": 2},
    ],
)
def test_rechaza_limites_incompatibles_de_restricciones(
    documento: Objeto,
    tmp_path: Path,
    rango: Objeto,
) -> None:
    hoja(documento)["protectedRanges"] = [{"range": rango}]
    sesion = SesionSimulada(documento)
    with pytest.raises(EstructuraError):
        aplicar(escritor(sesion, tmp_path), lector(sesion).revision())
    assert not sesion.envios


def test_lote_excesivo_no_se_fragmenta_ni_envia(
    documento: Objeto,
    tmp_path: Path,
) -> None:
    destino = hoja(documento)
    objeto(objeto(destino["properties"])["gridProperties"])["rowCount"] = 22000
    for indice in range(6, 22000):
        celda(destino, indice, 1)["userEnteredValue"] = {"stringValue": "Cliente Sintético"}
    sesion = SesionSimulada(documento)
    with pytest.raises(EstructuraError, match="lote excede"):
        aplicar(escritor(sesion, tmp_path), lector(sesion).revision())
    assert not sesion.envios
