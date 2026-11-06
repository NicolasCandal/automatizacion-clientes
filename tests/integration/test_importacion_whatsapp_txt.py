"""Importación de archivos sintéticos, salida atómica y conexión al flujo Excel."""

import json
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, date, datetime
from io import BytesIO
from os import environ
from pathlib import Path
from typing import cast

import pytest
from openpyxl import Workbook
from openpyxl.worksheet.worksheet import Worksheet
from pytest_mock import MockerFixture

from automatizacion_clientes.config import Config, cargar_config
from automatizacion_clientes.core.contratos import AlmacenSemanaContactos, ExtractorContactos
from automatizacion_clientes.core.errores import (
    ConcurrenciaError,
    ConfiguracionError,
    EstructuraError,
    PersistenciaError,
    ValidacionError,
)
from automatizacion_clientes.core.importacion_whatsapp import analizar_actividad
from automatizacion_clientes.core.modelos import PeriodoSemanal
from automatizacion_clientes.infra import almacen_semana_contactos_json as almacen_modulo
from automatizacion_clientes.infra import extractor_whatsapp_txt as modulo
from automatizacion_clientes.infra.almacen_semana_contactos_json import AlmacenSemanaContactosJson
from automatizacion_clientes.infra.extractor_whatsapp_txt import ExtractorWhatsAppTxt
from automatizacion_clientes.infra.lector_contactos_json import LectorContactosJson
from automatizacion_clientes.main import ejecutar_excel, validar_rutas
from automatizacion_clientes.services.importacion_whatsapp import ServicioImportacionWhatsApp

type Objeto = dict[str, object]
PERIODO = PeriodoSemanal(desde=date(2026, 11, 2), hasta=date(2026, 11, 6))


class RelojFijo:
    def ahora(self) -> datetime:
        return datetime(2026, 11, 6, 18, tzinfo=UTC)


@pytest.fixture(autouse=True)
def aislar_entorno(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for nombre in tuple(environ):
        if nombre.upper().startswith("AC_"):
            monkeypatch.delenv(nombre)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def configuracion(tmp_path: Path) -> Config:
    configuracion = Config(directorio_base=tmp_path, _env_file=None)
    carpeta = configuracion.whatsapp_manifiesto_path.parent
    carpeta.mkdir(parents=True)
    (carpeta / "axa.txt").write_text(
        "02/11/2026, 10:00 - Axa: contenido privado uno\n"
        "05/11/2026, 10:00 - Yo: contenido privado dos\n"
        "10/11/2026, 10:00 - Axa: posterior al período\n",
        encoding="utf-8",
    )
    (carpeta / "grupo.txt").write_bytes(
        "\ufeff[03/11/2026, 8:00:30 p. m.] Yo: mensaje de grupo\r\n".encode("utf-8")
    )
    (carpeta / "inactivo.txt").write_text(
        "01/11/2026, 10:00 - Notuka: antes del período\n", encoding="utf-8"
    )
    escribir_manifiesto(
        configuracion,
        {
            "version_esquema": 1,
            "orden_fecha": "dia_mes",
            "chats": [
                {
                    "archivo": "axa.txt",
                    "nombre_crudo": "Axa Prueba (IOL)",
                    "telefono": "+5491100000000",
                    "es_grupo": False,
                    "remitentes": ["Yo", "Axa"],
                },
                {
                    "archivo": "grupo.txt",
                    "nombre_crudo": "Equipo Prueba",
                    "es_grupo": True,
                    "remitentes": ["Yo"],
                },
                {
                    "archivo": "inactivo.txt",
                    "nombre_crudo": "Notuka Ejemplo",
                    "es_grupo": False,
                    "remitentes": ["Notuka"],
                },
            ],
        },
    )
    return configuracion


def escribir_manifiesto(configuracion: Config, documento: object) -> None:
    configuracion.whatsapp_manifiesto_path.write_text(json.dumps(documento), encoding="utf-8")


def leer_manifiesto(configuracion: Config) -> Objeto:
    return cast(
        Objeto, json.loads(configuracion.whatsapp_manifiesto_path.read_text(encoding="utf-8"))
    )


def entradas(documento: Objeto) -> list[Objeto]:
    return cast(list[Objeto], documento["chats"])


def servicio(configuracion: Config) -> ServicioImportacionWhatsApp:
    extractor: ExtractorContactos = ExtractorWhatsAppTxt(configuracion.whatsapp_manifiesto_path)
    almacen: AlmacenSemanaContactos = AlmacenSemanaContactosJson(
        configuracion.contactos_path, manifiesto=configuracion.whatsapp_manifiesto_path
    )
    return ServicioImportacionWhatsApp(extractor=extractor, almacen=almacen, reloj=RelojFijo())


def archivos(base: Path) -> dict[Path, bytes]:
    return {ruta.relative_to(base): ruta.read_bytes() for ruta in base.rglob("*") if ruta.is_file()}


def test_simulacion_valida_y_no_guarda_archivos(configuracion: Config) -> None:
    originales = archivos(configuracion.directorio_base)
    resultado = servicio(configuracion).ejecutar(PERIODO)
    assert resultado.simulacion
    assert resultado.extraccion.total_chats_escaneados == 3
    assert resultado.extraccion.mensajes_reconocidos == 5
    assert len(resultado.extraccion.semana.contactos) == 2
    assert resultado.extraccion.semana.contactos[0].ultima_fecha == date(2026, 11, 5)
    assert resultado.extraccion.semana.contactos[1].es_grupo
    assert archivos(configuracion.directorio_base) == originales


def test_salida_es_compatible_con_lector_y_no_contiene_mensajes(
    configuracion: Config,
    caplog: pytest.LogCaptureFixture,
) -> None:
    originales = archivos(configuracion.whatsapp_manifiesto_path.parent)
    with caplog.at_level("INFO"):
        resultado = servicio(configuracion).ejecutar(PERIODO, simulacion=False)
    assert not resultado.simulacion
    assert LectorContactosJson(configuracion.contactos_path).leer() == resultado.extraccion.semana
    contenido = configuracion.contactos_path.read_text(encoding="utf-8")
    assert "contenido privado" not in contenido
    assert "mensaje de grupo" not in contenido
    assert "posterior al período" not in contenido
    assert "Axa Prueba" not in caplog.text
    assert "+54911" not in caplog.text
    assert str(configuracion.whatsapp_manifiesto_path) not in caplog.text
    assert archivos(configuracion.whatsapp_manifiesto_path.parent) == originales


def test_entrada_importada_alimenta_motor_y_excluye_grupos(configuracion: Config) -> None:
    servicio(configuracion).ejecutar(PERIODO, simulacion=False)
    libro = Workbook()
    inicial = libro.active
    assert isinstance(inicial, Worksheet)
    libro.remove(inicial)
    for nombre in ("Inviu", "IOL", "Bull", "Balanz", "PPI"):
        destino = libro.create_sheet(nombre)
        destino.cell(1, 1, "Cliente")
        destino.cell(2, 4 if nombre == "Balanz" else 3, "Semana")
        destino.cell(3, 1, "Axa Prueba")
    libro.save(configuracion.excel_path)
    libro.close()
    originales = archivos(configuracion.directorio_base)
    resultado = ejecutar_excel(configuracion, encabezado="Semana")
    assert len(resultado.resolucion.resueltos) == 1
    assert len(resultado.resolucion.ignorados) == 1
    assert resultado.resolucion.resueltos[0].ubicaciones[0].hoja == "IOL"
    assert archivos(configuracion.directorio_base) == originales


def test_un_archivo_invalido_conserva_salida_anterior(configuracion: Config) -> None:
    configuracion.contactos_path.write_bytes(b"salida anterior")
    (configuracion.whatsapp_manifiesto_path.parent / "grupo.txt").write_bytes(b"invalido")
    with pytest.raises(EstructuraError):
        servicio(configuracion).ejecutar(PERIODO, simulacion=False)
    assert configuracion.contactos_path.read_bytes() == b"salida anterior"


def test_error_de_guardado_no_se_reintenta_como_importacion_completa(
    configuracion: Config,
    mocker: MockerFixture,
) -> None:
    configuracion.contactos_path.write_bytes(b"salida anterior")
    extraer = mocker.spy(ExtractorWhatsAppTxt, "extraer")
    guardar = mocker.patch.object(
        almacen_modulo, "guardar_json", side_effect=PersistenciaError("ficticio")
    )
    with pytest.raises(PersistenciaError):
        servicio(configuracion).ejecutar(PERIODO, simulacion=False)
    extraer.assert_called_once()
    guardar.assert_called_once()
    assert configuracion.contactos_path.read_bytes() == b"salida anterior"


@pytest.mark.parametrize(
    "documento", [[], {}, {"version_esquema": True, "orden_fecha": "dia_mes", "chats": []}]
)
def test_manifiesto_incompatible_se_rechaza(configuracion: Config, documento: object) -> None:
    escribir_manifiesto(configuracion, documento)
    with pytest.raises(EstructuraError):
        servicio(configuracion).ejecutar(PERIODO)


@pytest.mark.parametrize(
    ("campo", "valor"),
    [
        ("version_esquema", 2),
        ("orden_fecha", "automatico"),
        ("siglo_anio_corto", True),
        ("chats", []),
        ("chats", "secreto"),
        ("desconocido", 3),
    ],
)
def test_campos_invalidos_del_manifiesto_no_generan_salida(
    configuracion: Config,
    campo: str,
    valor: object,
) -> None:
    documento = leer_manifiesto(configuracion)
    documento[campo] = valor
    escribir_manifiesto(configuracion, documento)
    with pytest.raises(EstructuraError):
        servicio(configuracion).ejecutar(PERIODO)
    assert not configuracion.contactos_path.exists()


@pytest.mark.parametrize(
    ("campo", "valor"),
    [
        ("archivo", "../fuera.txt"),
        ("archivo", "chat.zip"),
        ("archivo", "chat.txt:oculto.txt"),
        ("archivo", "chat\u0000.txt"),
        ("archivo", ""),
        ("nombre_crudo", 42),
        ("es_grupo", "false"),
        ("telefono", 123),
        ("remitentes", []),
        ("remitentes", [5]),
        ("desconocido", None),
    ],
)
def test_campos_invalidos_de_chat_se_rechazan_antes_de_leer_txt(
    configuracion: Config,
    campo: str,
    valor: object,
    mocker: MockerFixture,
) -> None:
    documento = leer_manifiesto(configuracion)
    entradas(documento)[0][campo] = valor
    escribir_manifiesto(configuracion, documento)
    leer = mocker.spy(ExtractorWhatsAppTxt, "_lineas")
    with pytest.raises(EstructuraError):
        servicio(configuracion).ejecutar(PERIODO)
    leer.assert_not_called()


@pytest.mark.parametrize("caso", ["nombre", "telefono", "duplicados", "remitente_blanco"])
def test_identidades_y_remitentes_vacios_no_se_omiten(configuracion: Config, caso: str) -> None:
    documento = leer_manifiesto(configuracion)
    chat = entradas(documento)[0]
    if caso == "nombre":
        chat.update(nombre_crudo="", telefono=None)
    elif caso == "telefono":
        chat["telefono"] = " "
    else:
        chat["remitentes"] = ["Yo", "Yo"] if caso == "duplicados" else [" "]
    escribir_manifiesto(configuracion, documento)
    with pytest.raises(ValidacionError):
        servicio(configuracion).ejecutar(PERIODO)


def test_rutas_repetidas_y_absolutas_se_rechazan(configuracion: Config) -> None:
    original = leer_manifiesto(configuracion)
    duplicado = deepcopy(original)
    entradas(duplicado).append(deepcopy(entradas(duplicado)[0]))
    escribir_manifiesto(configuracion, duplicado)
    with pytest.raises(EstructuraError):
        servicio(configuracion).ejecutar(PERIODO)
    entradas(original)[0]["archivo"] = str(
        configuracion.whatsapp_manifiesto_path.parent / "axa.txt"
    )
    escribir_manifiesto(configuracion, original)
    with pytest.raises(EstructuraError):
        servicio(configuracion).ejecutar(PERIODO)


@pytest.mark.parametrize(
    "contenido", [b'{"chats": [], "chats": []}', b"{invalido", b"\xff", b" " * (1024 * 1024 + 1)]
)
def test_json_invalido_duplicado_o_excesivo_se_rechaza(
    configuracion: Config, contenido: bytes
) -> None:
    configuracion.whatsapp_manifiesto_path.write_bytes(contenido)
    with pytest.raises(EstructuraError):
        servicio(configuracion).ejecutar(PERIODO)


@pytest.mark.parametrize(
    "opciones",
    [
        {"limite_chats": 1},
        {"limite_archivo_bytes": 10},
        {"limite_total_bytes": 10},
        {"limite_linea_bytes": 10},
    ],
)
def test_limites_no_truncan_entradas(configuracion: Config, opciones: dict[str, int]) -> None:
    with pytest.raises(EstructuraError):
        ExtractorWhatsAppTxt(configuracion.whatsapp_manifiesto_path, **opciones).extraer(PERIODO)


@pytest.mark.parametrize("limite", [0, -1, True])
def test_limites_invalidos_no_abren_archivos(tmp_path: Path, limite: int) -> None:
    with pytest.raises(ConfiguracionError):
        ExtractorWhatsAppTxt(tmp_path / "ausente.json", limite_chats=limite)


@pytest.mark.parametrize("caso", ["ausente", "directorio", "utf8"])
def test_errores_de_entrada_se_traducen(configuracion: Config, caso: str) -> None:
    ruta = configuracion.whatsapp_manifiesto_path.parent / "axa.txt"
    ruta.unlink()
    if caso == "directorio":
        ruta.mkdir()
    elif caso == "utf8":
        ruta.write_bytes(b"\xff\xfe")
    with pytest.raises(PersistenciaError if caso == "ausente" else EstructuraError):
        servicio(configuracion).ejecutar(PERIODO)


@pytest.mark.parametrize("cambiar_manifiesto", [False, True])
def test_cambios_durante_lectura_impiden_guardar(
    configuracion: Config,
    mocker: MockerFixture,
    cambiar_manifiesto: bool,
) -> None:
    analizar = analizar_actividad
    cambio = False

    def analizar_y_cambiar(*argumentos: object, **opciones: object) -> object:
        nonlocal cambio
        resultado = cast(Callable[..., object], analizar)(*argumentos, **opciones)
        if not cambio:
            ruta = (
                configuracion.whatsapp_manifiesto_path
                if cambiar_manifiesto
                else configuracion.whatsapp_manifiesto_path.parent / "axa.txt"
            )
            ruta.write_bytes(ruta.read_bytes() + b"\n")
            cambio = True
        return resultado

    mocker.patch.object(modulo, "analizar_actividad", side_effect=analizar_y_cambiar)
    with pytest.raises(ConcurrenciaError):
        servicio(configuracion).ejecutar(PERIODO, simulacion=False)
    assert not configuracion.contactos_path.exists()


def test_archivo_se_cierra_si_falla_interpretacion(
    configuracion: Config, mocker: MockerFixture
) -> None:
    abrir = Path.open
    flujo = BytesIO(b"32/11/2026, 10:00 - Axa: secreto\n")
    ruta_chat = configuracion.whatsapp_manifiesto_path.parent / "axa.txt"

    def abrir_simulado(self: Path, *argumentos: object, **opciones: object) -> object:
        if self == ruta_chat:
            return flujo
        return cast(Callable[..., object], abrir)(self, *argumentos, **opciones)

    mocker.patch.object(Path, "open", autospec=True, side_effect=abrir_simulado)
    with pytest.raises(ValidacionError):
        servicio(configuracion).ejecutar(PERIODO)
    assert flujo.closed


def test_destino_no_puede_ser_txt_o_el_manifiesto(configuracion: Config) -> None:
    for destino in (
        configuracion.whatsapp_manifiesto_path,
        configuracion.whatsapp_manifiesto_path.parent / "axa.txt",
    ):
        with pytest.raises(ConfiguracionError):
            AlmacenSemanaContactosJson(destino, manifiesto=configuracion.whatsapp_manifiesto_path)


def test_reloj_sin_zona_no_guarda(configuracion: Config, mocker: MockerFixture) -> None:
    reloj = mocker.Mock()
    reloj.ahora.return_value = datetime(2026, 11, 6)
    almacen = mocker.Mock()
    importador = ServicioImportacionWhatsApp(
        extractor=ExtractorWhatsAppTxt(configuracion.whatsapp_manifiesto_path),
        almacen=almacen,
        reloj=reloj,
    )
    with pytest.raises(ConfiguracionError):
        importador.ejecutar(PERIODO, simulacion=False)
    almacen.guardar.assert_not_called()


def test_ruta_nueva_se_configura_y_protege_sin_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AC_WHATSAPP_MANIFIESTO_PATH", "exportaciones/lote.json")
    configuracion = cargar_config(ruta_env=None, directorio_base=tmp_path)
    assert configuracion.whatsapp_manifiesto_path == tmp_path / "exportaciones/lote.json"
    assert not configuracion.whatsapp_manifiesto_path.parent.exists()
    with pytest.raises(ConfiguracionError):
        validar_rutas(
            configuracion.model_copy(
                update={"whatsapp_manifiesto_path": configuracion.contactos_path}
            )
        )
