"""Escrituras en XLSX sintéticos, respaldos y protección frente a cambios externos."""

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from openpyxl import Workbook, load_workbook
from openpyxl.cell.rich_text import CellRichText, TextBlock
from openpyxl.cell.text import InlineFont
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.worksheet import Worksheet
from pytest_mock import MockerFixture

from automatizacion_clientes.core.contratos import EscritorClientes
from automatizacion_clientes.core.errores import (
    ConcurrenciaError,
    ConfiguracionError,
    EstructuraError,
    PersistenciaError,
)
from automatizacion_clientes.infra import escritor_clientes_excel
from automatizacion_clientes.infra.escritor_clientes_excel import EscritorClientesExcel
from automatizacion_clientes.infra.lector_clientes_excel import LectorClientesExcel


class RelojFijo:
    def ahora(self) -> datetime:
        return datetime(2026, 11, 5, 12, 30, tzinfo=UTC)


def obtener_hoja(libro: Workbook, nombre: str = "IOL") -> Worksheet:
    hoja = libro[nombre]
    assert isinstance(hoja, Worksheet)
    return hoja


def editar(ruta: Path, modificar: Callable[[Workbook], None]) -> None:
    libro = load_workbook(ruta, data_only=False)
    try:
        modificar(libro)
        libro.save(ruta)
    finally:
        libro.close()


def revision(ruta: Path) -> str:
    with LectorClientesExcel(ruta) as lector:
        return lector.revision()


@pytest.fixture
def ruta_excel(tmp_path: Path) -> Path:
    libro = Workbook()
    inicial = libro.active
    assert isinstance(inicial, Worksheet)
    libro.remove(inicial)
    for nombre in ("Inviu", "IOL", "Bull", "Balanz", "PPI"):
        hoja: Worksheet = libro.create_sheet(nombre)
        hoja.cell(1, 1, "Cliente")
        hoja.cell(2, 4 if nombre == "Balanz" else 3, "Semana anterior")
        hoja.cell(3, 1, "Axa Prueba")
    hoja = obtener_hoja(libro)
    hoja.cell(5, 1, "Notuka Ejemplo")
    hoja.cell(8, 1, "Axa Prueba y/o Notuka Ejemplo")
    hoja.cell(3, 2, "=100+200")
    hoja.cell(2, 4, " Semana actual ")
    hoja.cell(3, 4, "x")
    hoja.cell(5, 4).fill = PatternFill(patternType="solid", fgColor="FF76923C")
    hoja.cell(4, 4, "Nota fuera de clientes")
    hoja.cell(3, 3, "=1+2")
    hoja.cell(2, 4).font = Font(bold=True, color="FF123456")
    hoja.cell(2, 4).alignment = Alignment(horizontal="center")
    hoja.cell(2, 4).number_format = "@"
    hoja.column_dimensions["D"].width = 23
    hoja.cell(8, 4).font = Font(italic=True)
    obtener_hoja(libro, "Balanz").cell(3, 3, "=50+50")
    ruta = tmp_path / "clientes.xlsx"
    try:
        libro.save(ruta)
    finally:
        libro.close()
    return ruta


@pytest.fixture
def escritor(ruta_excel: Path, tmp_path: Path) -> EscritorClientesExcel:
    return EscritorClientesExcel(
        ruta_excel,
        directorio_respaldos=tmp_path / "respaldos",
        reloj=RelojFijo(),
    )


def test_constructor_no_accede_al_disco(tmp_path: Path, mocker: MockerFixture) -> None:
    lectura = mocker.patch.object(Path, "read_bytes")
    escritor: EscritorClientes = EscritorClientesExcel(
        tmp_path / "ausente.XLSX",
        directorio_respaldos=tmp_path / "copias",
        reloj=RelojFijo(),
    )
    assert escritor is not None
    lectura.assert_not_called()
    assert not (tmp_path / "copias").exists()


@pytest.mark.parametrize("extension", [".xls", ".xlsm", ".csv", ""])
def test_rechaza_extensiones_no_admitidas(tmp_path: Path, extension: str) -> None:
    with pytest.raises(ConfiguracionError):
        EscritorClientesExcel(
            tmp_path / ("clientes" + extension),
            directorio_respaldos=tmp_path,
            reloj=RelojFijo(),
        )


def test_respaldo_exacto_y_exclusivo_aunque_el_reloj_se_repita(
    ruta_excel: Path,
    escritor: EscritorClientesExcel,
) -> None:
    original = ruta_excel.read_bytes()
    primero = escritor.crear_respaldo()
    segundo = escritor.crear_respaldo()
    assert primero != segundo
    assert primero.suffix == ".xlsx"
    assert primero.name.startswith("clientes_20261105T123000_000000Z_")
    assert primero.read_bytes() == segundo.read_bytes() == original
    assert ruta_excel.read_bytes() == original
    assert set(primero.parent.iterdir()) == {primero, segundo}


def test_rechaza_reloj_sin_zona_antes_de_crear_archivos(
    escritor: EscritorClientesExcel,
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    mocker.patch.object(RelojFijo, "ahora", return_value=datetime(2026, 11, 5))
    with pytest.raises(ConfiguracionError):
        escritor.crear_respaldo()
    assert not (tmp_path / "respaldos").exists()


def test_fallo_sincronizacion_respaldo_limpia_copia_incompleta(
    ruta_excel: Path,
    escritor: EscritorClientesExcel,
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    original = ruta_excel.read_bytes()
    causa = OSError("disco lleno")
    mocker.patch.object(escritor_clientes_excel, "fsync", side_effect=causa)
    with pytest.raises(PersistenciaError) as error:
        escritor.crear_respaldo()
    assert error.value.__cause__ is causa
    assert list((tmp_path / "respaldos").iterdir()) == []
    assert ruta_excel.read_bytes() == original


def test_respaldo_detecta_cambio_y_elimina_copia_no_confirmada(
    ruta_excel: Path,
    escritor: EscritorClientesExcel,
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    original = ruta_excel.read_bytes()
    mocker.patch.object(Path, "read_bytes", side_effect=[original, b"version externa"])
    with pytest.raises(ConcurrenciaError):
        escritor.crear_respaldo()
    assert list((tmp_path / "respaldos").iterdir()) == []


def test_actualiza_solo_semana_elegida_y_preserva_huecos_y_formulas(
    ruta_excel: Path,
    escritor: EscritorClientesExcel,
) -> None:
    escritor.aplicar_marcas(
        "IOL", 4, "Semana actual", frozenset({8}), revision_esperada=revision(ruta_excel)
    )
    with LectorClientesExcel(ruta_excel) as lector:
        assert lector.leer_marcas("IOL", 4) == frozenset({8})
        assert tuple(cliente.fila for cliente in lector.leer_clientes("IOL")) == (3, 5, 8)
    libro = load_workbook(ruta_excel, data_only=False)
    try:
        hoja = obtener_hoja(libro)
        assert hoja.cell(2, 4).value == " Semana actual "
        assert hoja.cell(3, 2).value == "=100+200"
        assert hoja.cell(3, 3).value == "=1+2"
        assert hoja.cell(4, 4).value == "Nota fuera de clientes"
        assert hoja.cell(3, 4).value is None
        assert hoja.cell(5, 4).fill.fgColor.rgb == "FFFFFFFF"
        assert hoja.cell(8, 4).fill.fgColor.rgb == "FF76923C"
        assert hoja.cell(8, 4).font.italic
        assert obtener_hoja(libro, "Balanz").cell(3, 3).value == "=50+50"
    finally:
        libro.close()


def test_repeticion_no_crea_otra_semana_y_vacia_seleccion_quita_marcas(
    ruta_excel: Path,
    escritor: EscritorClientesExcel,
) -> None:
    for _ in range(2):
        escritor.aplicar_marcas(
            "IOL",
            4,
            "Semana actual",
            frozenset({3, 8}),
            revision_esperada=revision(ruta_excel),
        )
    with LectorClientesExcel(ruta_excel) as lector:
        assert lector.ultima_semana("IOL") == (4, " Semana actual ")
        assert lector.leer_marcas("IOL", 4) == frozenset({3, 8})
    escritor.aplicar_marcas(
        "IOL", 4, "Semana actual", frozenset(), revision_esperada=revision(ruta_excel)
    )
    with LectorClientesExcel(ruta_excel) as lector:
        assert lector.leer_marcas("IOL", 4) == frozenset()


def test_anade_semana_copia_solo_formato_y_ancho_del_encabezado_anterior(
    ruta_excel: Path,
    escritor: EscritorClientesExcel,
) -> None:
    escritor.aplicar_marcas(
        "IOL", 5, " Semana nueva ", frozenset({5}), revision_esperada=revision(ruta_excel)
    )
    libro = load_workbook(ruta_excel, data_only=False)
    try:
        hoja = obtener_hoja(libro)
        assert hoja.cell(2, 5).value == "Semana nueva"
        assert hoja.cell(2, 5).style_id == hoja.cell(2, 4).style_id
        assert hoja.cell(2, 5).number_format == "@"
        assert hoja.column_dimensions["E"].width == 23
        assert hoja.cell(3, 4).value == "x"
    finally:
        libro.close()
    with LectorClientesExcel(ruta_excel) as lector:
        assert lector.leer_marcas("IOL", 5) == frozenset({5})


@pytest.mark.parametrize(("nombre", "primera"), [("PPI", 3), ("Balanz", 4)])
def test_primera_semana_respeta_columnas_fijas(
    ruta_excel: Path,
    escritor: EscritorClientesExcel,
    nombre: str,
    primera: int,
) -> None:
    def quitar_semana(libro: Workbook) -> None:
        obtener_hoja(libro, nombre).cell(2, primera).value = None

    editar(ruta_excel, quitar_semana)
    escritor.aplicar_marcas(
        nombre, primera, "Primera", frozenset({3}), revision_esperada=revision(ruta_excel)
    )
    with LectorClientesExcel(ruta_excel) as lector:
        assert lector.ultima_semana(nombre) == (primera, "Primera")
        assert lector.leer_marcas(nombre, primera) == frozenset({3})


@pytest.mark.parametrize(
    ("columna", "encabezado", "filas"),
    [
        (2, "Semana nueva", frozenset({3})),
        (4, "Semana nueva", frozenset({3})),
        (6, "Semana nueva", frozenset({3})),
        (5, "Semana actual", frozenset({3})),
        (4, "Semana actual", frozenset({4})),
        (4, "Semana actual", frozenset({999})),
        (4, "Semana actual", frozenset({2})),
        (4, "Semana actual", frozenset({True})),
        (0, "Semana actual", frozenset({3})),
        (16385, "Semana actual", frozenset({3})),
        (True, "Semana actual", frozenset({3})),
        (4, " ", frozenset({3})),
        (4, "=1+2", frozenset({3})),
        (4, "#REF!", frozenset({3})),
        (4, "Semana\x00", frozenset({3})),
        (4, "a" * 32768, frozenset({3})),
    ],
)
def test_peticion_invalida_no_cambia_archivo(
    ruta_excel: Path,
    escritor: EscritorClientesExcel,
    columna: int,
    encabezado: str,
    filas: frozenset[int],
) -> None:
    original = ruta_excel.read_bytes()
    with pytest.raises(EstructuraError):
        escritor.aplicar_marcas(
            "IOL", columna, encabezado, filas, revision_esperada=revision(ruta_excel)
        )
    assert ruta_excel.read_bytes() == original
    assert not list(ruta_excel.parent.glob(".excel-*"))


@pytest.mark.parametrize("hoja", ["Otra", "iol"])
def test_rechaza_hoja_no_configurada(escritor: EscritorClientesExcel, hoja: str) -> None:
    with pytest.raises(EstructuraError):
        escritor.aplicar_marcas(hoja, 4, "Semana actual", frozenset(), revision_esperada="revision")


def test_rechaza_revision_vacia(escritor: EscritorClientesExcel) -> None:
    with pytest.raises(EstructuraError):
        escritor.aplicar_marcas("IOL", 4, "Semana actual", frozenset(), revision_esperada=" ")


def test_revision_obsoleta_no_carga_ni_guarda(
    ruta_excel: Path,
    escritor: EscritorClientesExcel,
    mocker: MockerFixture,
) -> None:
    original = ruta_excel.read_bytes()
    carga = mocker.spy(escritor_clientes_excel, "load_workbook")
    with pytest.raises(ConcurrenciaError):
        escritor.aplicar_marcas(
            "IOL", 4, "Semana actual", frozenset({3}), revision_esperada="obsoleta"
        )
    carga.assert_not_called()
    assert ruta_excel.read_bytes() == original


@pytest.mark.parametrize("valor", ["=1+2", "Otro dato", 10, "#REF!"])
def test_rechaza_datos_ajenos_o_formulas_en_cualquier_cliente_de_la_semana(
    ruta_excel: Path,
    escritor: EscritorClientesExcel,
    valor: str | int,
) -> None:
    editar(ruta_excel, lambda libro: setattr(obtener_hoja(libro).cell(8, 4), "value", valor))
    original = ruta_excel.read_bytes()
    with pytest.raises(EstructuraError):
        escritor.aplicar_marcas(
            "IOL", 4, "Semana actual", frozenset({3}), revision_esperada=revision(ruta_excel)
        )
    assert ruta_excel.read_bytes() == original


@pytest.mark.parametrize(
    "caso",
    [
        "protegida",
        "combinada",
        "duplicada",
        "falta_hoja",
        "nombre",
        "formula_nombre",
        "formula_semana",
    ],
)
def test_rechaza_estructuras_ambiguas_sin_guardar(
    ruta_excel: Path,
    escritor: EscritorClientesExcel,
    caso: str,
) -> None:
    def modificar(libro: Workbook) -> None:
        hoja = obtener_hoja(libro)
        if caso == "protegida":
            hoja.protection.sheet = True
        elif caso == "combinada":
            hoja.merge_cells("D8:D9")
        elif caso == "duplicada":
            hoja.cell(2, 5, "Semana actual")
        elif caso == "falta_hoja":
            libro.remove(obtener_hoja(libro, "PPI"))
        elif caso == "nombre":
            hoja.cell(1, 1, "Desconocido")
        elif caso == "formula_nombre":
            hoja.cell(8, 1, "=A3")
        else:
            hoja.cell(2, 5, "=D2")

    editar(ruta_excel, modificar)
    original = ruta_excel.read_bytes()
    with pytest.raises(EstructuraError):
        escritor.aplicar_marcas(
            "IOL", 4, "Semana actual", frozenset({3}), revision_esperada=revision(ruta_excel)
        )
    assert ruta_excel.read_bytes() == original


def test_detecta_cambio_durante_serializacion_y_conserva_version_externa(
    ruta_excel: Path,
    escritor: EscritorClientesExcel,
    mocker: MockerFixture,
) -> None:
    esperada = revision(ruta_excel)
    serializar = escritor_clientes_excel._serializar
    externa: list[bytes] = []

    def serializar_y_cambiar(libro: Workbook) -> bytes:
        contenido = serializar(libro)
        editar(ruta_excel, lambda otro: setattr(obtener_hoja(otro).cell(5, 2), "value", 1234))
        externa.append(ruta_excel.read_bytes())
        return contenido

    mocker.patch.object(escritor_clientes_excel, "_serializar", side_effect=serializar_y_cambiar)
    with pytest.raises(ConcurrenciaError):
        escritor.aplicar_marcas(
            "IOL", 4, "Semana actual", frozenset({3}), revision_esperada=esperada
        )
    assert ruta_excel.read_bytes() == externa[0]
    assert not list(ruta_excel.parent.glob(".excel-*"))


@pytest.mark.parametrize("operacion", ["serializar", "sincronizar", "reemplazar"])
def test_fallos_de_guardado_conservan_original_y_limpian_temporal(
    ruta_excel: Path,
    escritor: EscritorClientesExcel,
    mocker: MockerFixture,
    operacion: str,
) -> None:
    original = ruta_excel.read_bytes()
    causa = OSError("fallo simulado")
    if operacion == "serializar":
        mocker.patch.object(escritor_clientes_excel, "_serializar", side_effect=causa)
    elif operacion == "sincronizar":
        mocker.patch.object(escritor_clientes_excel, "fsync", side_effect=causa)
    else:
        reemplazo = mocker.patch.object(Path, "replace", side_effect=causa)
    with pytest.raises(PersistenciaError) as error:
        escritor.aplicar_marcas(
            "IOL", 4, "Semana actual", frozenset({3}), revision_esperada=revision(ruta_excel)
        )
    assert error.value.__cause__ is causa
    assert ruta_excel.read_bytes() == original
    assert not list(ruta_excel.parent.glob(".excel-*"))
    if operacion == "reemplazar":
        assert reemplazo.call_count == 1


def bloqueo_windows() -> OSError:
    causa = OSError("uso compartido")
    causa.winerror = 32
    return causa


def test_reintenta_solo_bloqueo_transitorio_y_vuelve_a_comprobar_revision(
    ruta_excel: Path,
    escritor: EscritorClientesExcel,
    mocker: MockerFixture,
) -> None:
    reemplazar = Path.replace
    llamadas = 0

    def reemplazar_con_bloqueo(temporal: Path, destino: Path) -> Path:
        nonlocal llamadas
        llamadas += 1
        if llamadas == 1:
            raise bloqueo_windows()
        return reemplazar(temporal, destino)

    mocker.patch.object(Path, "replace", autospec=True, side_effect=reemplazar_con_bloqueo)
    comprobacion = mocker.spy(escritor, "_comprobar_revision")
    escritor.aplicar_marcas(
        "IOL", 4, "Semana actual", frozenset({8}), revision_esperada=revision(ruta_excel)
    )
    assert llamadas == comprobacion.call_count == 2
    with LectorClientesExcel(ruta_excel) as lector:
        assert lector.leer_marcas("IOL", 4) == frozenset({8})


def test_bloqueo_persistente_se_limita_a_cuatro_intentos(
    ruta_excel: Path,
    escritor: EscritorClientesExcel,
    mocker: MockerFixture,
) -> None:
    original = ruta_excel.read_bytes()
    reemplazo = mocker.patch.object(Path, "replace", side_effect=bloqueo_windows())
    with pytest.raises(PersistenciaError):
        escritor.aplicar_marcas(
            "IOL", 4, "Semana actual", frozenset({3}), revision_esperada=revision(ruta_excel)
        )
    assert reemplazo.call_count == 4
    assert ruta_excel.read_bytes() == original
    assert not list(ruta_excel.parent.glob(".excel-*"))


def test_cambio_entre_reintentos_cancela_sin_sobrescribir(
    ruta_excel: Path,
    escritor: EscritorClientesExcel,
    mocker: MockerFixture,
) -> None:
    externa: list[bytes] = []

    def bloquear_y_cambiar(temporal: Path, destino: Path) -> Path:
        editar(destino, lambda libro: setattr(obtener_hoja(libro).cell(5, 2), "value", 999))
        externa.append(destino.read_bytes())
        raise bloqueo_windows()

    reemplazo = mocker.patch.object(Path, "replace", autospec=True, side_effect=bloquear_y_cambiar)
    with pytest.raises(ConcurrenciaError):
        escritor.aplicar_marcas(
            "IOL", 4, "Semana actual", frozenset({8}), revision_esperada=revision(ruta_excel)
        )
    assert reemplazo.call_count == 1
    assert ruta_excel.read_bytes() == externa[0]
    assert not list(ruta_excel.parent.glob(".excel-*"))


@pytest.mark.parametrize("operacion", ["respaldo", "marcas"])
def test_archivo_ausente_es_error_de_persistencia(
    ruta_excel: Path,
    escritor: EscritorClientesExcel,
    operacion: str,
) -> None:
    ruta_excel.unlink()
    with pytest.raises(PersistenciaError) as error:
        if operacion == "respaldo":
            escritor.crear_respaldo()
        else:
            escritor.aplicar_marcas(
                "IOL", 4, "Semana actual", frozenset(), revision_esperada="esperada"
            )
    assert isinstance(error.value.__cause__, FileNotFoundError)


def test_archivo_corrupto_no_se_sobrescribe(
    ruta_excel: Path, escritor: EscritorClientesExcel
) -> None:
    ruta_excel.write_bytes(b"no es un libro")
    with pytest.raises(EstructuraError):
        escritor.aplicar_marcas(
            "IOL", 4, "Semana actual", frozenset(), revision_esperada=revision(ruta_excel)
        )
    assert ruta_excel.read_bytes() == b"no es un libro"


def test_rechaza_componentes_que_podrian_perderse_al_guardar(
    ruta_excel: Path,
    escritor: EscritorClientesExcel,
) -> None:
    with ZipFile(ruta_excel, "a", compression=ZIP_DEFLATED) as paquete:
        paquete.writestr("xl/drawings/drawing1.xml", "<dibujos/>")
    original = ruta_excel.read_bytes()
    with pytest.raises(EstructuraError, match="tabulares"):
        escritor.aplicar_marcas(
            "IOL", 4, "Semana actual", frozenset({3}), revision_esperada=revision(ruta_excel)
        )
    assert ruta_excel.read_bytes() == original


def test_conserva_texto_enriquecido_en_nombres_y_columnas_fijas(
    ruta_excel: Path,
    escritor: EscritorClientesExcel,
) -> None:
    def enriquecer(libro: Workbook) -> None:
        hoja = obtener_hoja(libro)
        hoja.cell(5, 1).value = CellRichText(TextBlock(InlineFont(b=True), "Notuka Ejemplo"))
        hoja.cell(5, 2).value = CellRichText(TextBlock(InlineFont(i=True), "Detalle"))

    editar(ruta_excel, enriquecer)
    escritor.aplicar_marcas(
        "IOL", 4, "Semana actual", frozenset({5}), revision_esperada=revision(ruta_excel)
    )
    libro = load_workbook(ruta_excel, rich_text=True)
    try:
        nombre = obtener_hoja(libro).cell(5, 1).value
        detalle = obtener_hoja(libro).cell(5, 2).value
        assert isinstance(nombre, CellRichText)
        assert isinstance(detalle, CellRichText)
        assert str(nombre) == "Notuka Ejemplo"
        assert str(detalle) == "Detalle"
    finally:
        libro.close()


def test_logs_no_incluyen_nombres_rutas_ni_encabezados(
    ruta_excel: Path,
    escritor: EscritorClientesExcel,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("INFO"):
        escritor.crear_respaldo()
        escritor.aplicar_marcas(
            "IOL", 4, "Semana actual", frozenset({3}), revision_esperada=revision(ruta_excel)
        )
    assert "Respaldo Excel creado" in caplog.text
    assert "1 clientes marcados" in caplog.text
    assert "Axa Prueba" not in caplog.text
    assert "Semana actual" not in caplog.text
    assert str(ruta_excel) not in caplog.text
