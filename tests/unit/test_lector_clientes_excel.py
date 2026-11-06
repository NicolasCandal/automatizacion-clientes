"""Lectura de libros sintéticos, marcas y ciclo de vida de la instantánea."""

import re
from collections.abc import Callable
from io import BytesIO
from os import utime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Color, PatternFill
from openpyxl.worksheet.worksheet import Worksheet
from pytest_mock import MockerFixture

from automatizacion_clientes.core.contratos import LectorClientes
from automatizacion_clientes.core.errores import (
    ConfiguracionError,
    EstructuraError,
    PersistenciaError,
)
from automatizacion_clientes.infra import lector_clientes_excel
from automatizacion_clientes.infra.lector_clientes_excel import LectorClientesExcel


def obtener_hoja(libro: Workbook, nombre: str) -> Worksheet:
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


@pytest.fixture
def ruta_excel(tmp_path: Path) -> Path:
    libro = Workbook()
    inicial = libro.active
    assert isinstance(inicial, Worksheet)
    libro.remove(inicial)
    for nombre in ("Inviu", "IOL", "Bull", "Balanz", "PPI"):
        hoja: Worksheet = libro.create_sheet(nombre)
        hoja.cell(1, 1, "CLIENTES" if nombre == "Balanz" else "Cliente")
        hoja.cell(1, 2, "Capital")
        if nombre != "PPI":
            hoja.cell(2, 4 if nombre == "Balanz" else 3, "02 al 06-11")
            hoja.cell(3, 1, "Axa Prueba")
    balanz = obtener_hoja(libro, "Balanz")
    balanz.cell(2, 2, "Capital")
    balanz.cell(2, 3, "Capital 2")
    hoja = obtener_hoja(libro, "IOL")
    hoja.cell(2, 4, " 09 al 13-11 ")
    hoja.cell(3, 1, "  Axa Prueba  ")
    hoja.cell(3, 2, "=100+200")
    hoja.cell(5, 1, "Notuka Ejemplo")
    hoja.cell(6, 1, "   ")
    hoja.cell(8, 1, "AXA PRUEBA Y/O NOTUKA EJEMPLO")
    hoja.cell(3, 4).fill = PatternFill(patternType="solid", fgColor="FF76923C")
    hoja.cell(5, 4).fill = PatternFill(patternType="solid", fgColor="FFFFFFFF")
    hoja.cell(8, 4, " X ")
    hoja.cell(1000, 4).fill = PatternFill(patternType="solid", fgColor="FF76923C")
    ruta = tmp_path / "clientes.xlsx"
    try:
        libro.save(ruta)
    finally:
        libro.close()
    return ruta


def test_constructor_no_abre_archivos(tmp_path: Path, mocker: MockerFixture) -> None:
    lectura = mocker.patch.object(Path, "read_bytes", side_effect=PermissionError("bloqueado"))
    lector = LectorClientesExcel(tmp_path / "ausente.xlsx")
    lectura.assert_not_called()
    lector.cerrar()


def test_detecta_estructura_real_y_columnas_fijas_balanz(ruta_excel: Path) -> None:
    lector: LectorClientes = LectorClientesExcel(ruta_excel)
    hojas = lector.listar_hojas()
    por_nombre = {hoja.nombre: hoja for hoja in hojas}
    assert tuple(por_nombre) == ("Inviu", "IOL", "Bull", "Balanz", "PPI")
    assert por_nombre["IOL"].sheet_id == 1
    assert por_nombre["IOL"].primera_col_semana == 3
    assert por_nombre["IOL"].ultima_col_semana == 4
    assert por_nombre["IOL"].ultima_fila_con_cliente == 8
    assert por_nombre["Balanz"].primera_col_semana == 4
    assert por_nombre["PPI"].primera_col_semana is None
    assert por_nombre["PPI"].ultima_fila_con_cliente == 2


def test_conserva_clientes_despues_de_huecos_y_nombres_sin_recortar(ruta_excel: Path) -> None:
    with LectorClientesExcel(ruta_excel) as lector:
        clientes = lector.leer_clientes("IOL")
    assert tuple(cliente.fila for cliente in clientes) == (3, 5, 8)
    assert clientes[0].nombre_crudo == "  Axa Prueba  "
    assert clientes[2].nombre_crudo == "AXA PRUEBA Y/O NOTUKA EJEMPLO"


def test_marcas_incluyen_verde_y_x_y_excluyen_filas_sin_cliente(ruta_excel: Path) -> None:
    with LectorClientesExcel(ruta_excel) as lector:
        assert lector.leer_marcas("IOL", 4) == frozenset({3, 8})
        assert lector.leer_marcas("IOL", 3) == frozenset()


def test_ultima_semana_conserva_texto_y_ausencia_de_semanas(ruta_excel: Path) -> None:
    with LectorClientesExcel(ruta_excel) as lector:
        assert lector.ultima_semana("IOL") == (4, " 09 al 13-11 ")
        assert lector.ultima_semana("PPI") == (1, "")
        assert lector.leer_clientes("PPI") == ()


def test_encabezados_completos_conservan_orden_columnas_y_textos(ruta_excel: Path) -> None:
    with LectorClientesExcel(ruta_excel) as lector:
        assert lector.leer_encabezados("IOL") == ((3, "02 al 06-11"), (4, " 09 al 13-11 "))
        assert lector.leer_encabezados("Balanz") == ((4, "02 al 06-11"),)
        assert lector.leer_encabezados("PPI") == ()
    with pytest.raises(ConfiguracionError):
        lector.leer_encabezados("IOL")


def test_lectura_no_modifica_bytes_ni_formulas(ruta_excel: Path) -> None:
    original = ruta_excel.read_bytes()
    with LectorClientesExcel(ruta_excel) as lector:
        lector.listar_hojas()
        lector.leer_clientes("IOL")
        lector.leer_marcas("IOL", 4)
        lector.revision()
    assert ruta_excel.read_bytes() == original
    libro = load_workbook(ruta_excel, data_only=False)
    try:
        assert obtener_hoja(libro, "IOL").cell(3, 2).value == "=100+200"
    finally:
        libro.close()


def test_analiza_una_vez_y_refresca_por_contenido_aunque_no_cambie_mtime(
    ruta_excel: Path, mocker: MockerFixture
) -> None:
    carga = mocker.spy(lector_clientes_excel, "load_workbook")
    with LectorClientesExcel(ruta_excel) as lector:
        lector.listar_hojas()
        lector.leer_clientes("IOL")
        lector.leer_marcas("IOL", 4)
        lector.ultima_semana("IOL")
        assert carga.call_count == 1
        revision = lector.revision()
        sello = ruta_excel.stat()

        def modificar(libro: Workbook) -> None:
            obtener_hoja(libro, "IOL").cell(5, 1, "Nombre nuevo")

        editar(ruta_excel, modificar)
        utime(ruta_excel, ns=(sello.st_atime_ns, sello.st_mtime_ns))
        assert lector.revision() != revision
        assert lector.leer_clientes("IOL")[1].nombre_crudo == "Nombre nuevo"
        assert carga.call_count == 2


def test_revision_detecta_cambios_solo_de_formato(ruta_excel: Path) -> None:
    with LectorClientesExcel(ruta_excel) as lector:
        revision = lector.revision()

        def modificar(libro: Workbook) -> None:
            obtener_hoja(libro, "IOL").cell(5, 4).fill = PatternFill(
                patternType="solid", fgColor="FF76923C"
            )

        editar(ruta_excel, modificar)
        assert lector.revision() != revision
        assert lector.leer_marcas("IOL", 4) == frozenset({3, 5, 8})


@pytest.mark.parametrize("color", ["0076923C", "FF76923c"])
def test_verde_ignora_alfa_y_mayusculas(ruta_excel: Path, color: str) -> None:
    def modificar(libro: Workbook) -> None:
        obtener_hoja(libro, "IOL").cell(3, 4).fill = PatternFill(patternType="solid", fgColor=color)

    editar(ruta_excel, modificar)
    assert LectorClientesExcel(ruta_excel).leer_marcas("IOL", 4) == frozenset({3, 8})


def test_patron_no_solido_no_se_confunde_con_relleno_verde(ruta_excel: Path) -> None:
    def modificar(libro: Workbook) -> None:
        obtener_hoja(libro, "IOL").cell(3, 4).fill = PatternFill(
            patternType="darkGrid", fgColor="FF76923C"
        )

    editar(ruta_excel, modificar)
    assert LectorClientesExcel(ruta_excel).leer_marcas("IOL", 4) == frozenset({8})


@pytest.mark.parametrize("color", [Color(theme=5), Color(indexed=10)])
def test_colores_no_rgb_no_bloquean_catalogo_pero_no_se_ignoran_al_leer_marcas(
    ruta_excel: Path, color: Color
) -> None:
    def modificar(libro: Workbook) -> None:
        obtener_hoja(libro, "IOL").cell(5, 4).fill = PatternFill(patternType="solid", fgColor=color)

    editar(ruta_excel, modificar)
    with LectorClientesExcel(ruta_excel) as lector:
        assert len(lector.leer_clientes("IOL")) == 3
        with pytest.raises(EstructuraError, match="RGB"):
            lector.leer_marcas("IOL", 4)


def test_x_historica_es_valida_incluso_con_color_de_tema(ruta_excel: Path) -> None:
    def modificar(libro: Workbook) -> None:
        obtener_hoja(libro, "IOL").cell(8, 4).fill = PatternFill(
            patternType="solid", fgColor=Color(theme=5)
        )

    editar(ruta_excel, modificar)
    assert LectorClientesExcel(ruta_excel).leer_marcas("IOL", 4) == frozenset({3, 8})


@pytest.mark.parametrize("columna", [True, 0, -1, 16385, 2, 5])
def test_columna_de_marcas_debe_ser_una_semana_existente(ruta_excel: Path, columna: int) -> None:
    with LectorClientesExcel(ruta_excel) as lector, pytest.raises(EstructuraError):
        lector.leer_marcas("IOL", columna)


def test_no_admite_leer_marcas_de_hoja_sin_semanas(ruta_excel: Path) -> None:
    with LectorClientesExcel(ruta_excel) as lector, pytest.raises(EstructuraError):
        lector.leer_marcas("PPI", 3)


@pytest.mark.parametrize("valor", [123, True, "=B5", "#VALUE!"])
def test_nombres_no_pueden_ser_numeros_formulas_ni_errores(
    ruta_excel: Path, valor: int | str
) -> None:
    def modificar(libro: Workbook) -> None:
        obtener_hoja(libro, "IOL").cell(5, 1, valor)

    editar(ruta_excel, modificar)
    with pytest.raises(EstructuraError, match="texto literal"):
        LectorClientesExcel(ruta_excel).leer_clientes("IOL")


def test_encabezado_de_nombre_debe_estar_presente(ruta_excel: Path) -> None:
    def modificar(libro: Workbook) -> None:
        obtener_hoja(libro, "IOL").cell(1, 1).value = None

    editar(ruta_excel, modificar)
    with pytest.raises(EstructuraError, match="encabezado"):
        LectorClientesExcel(ruta_excel).listar_hojas()


def test_no_interpreta_formulas_como_encabezados_de_semana(ruta_excel: Path) -> None:
    def modificar(libro: Workbook) -> None:
        obtener_hoja(libro, "IOL").cell(2, 4, "=C2")

    editar(ruta_excel, modificar)
    with pytest.raises(EstructuraError, match="encabezados"):
        LectorClientesExcel(ruta_excel).ultima_semana("IOL")


def test_hojas_requeridas_faltantes_y_consultas_desconocidas_se_rechazan(ruta_excel: Path) -> None:
    with LectorClientesExcel(ruta_excel) as lector, pytest.raises(EstructuraError):
        lector.leer_clientes("Otra hoja")

    def modificar(libro: Workbook) -> None:
        libro.remove(obtener_hoja(libro, "PPI"))

    editar(ruta_excel, modificar)
    with pytest.raises(EstructuraError, match="requeridas"):
        LectorClientesExcel(ruta_excel).listar_hojas()


def test_dimensiones_incorrectas_no_ocultan_filas_reales(ruta_excel: Path) -> None:
    with BytesIO(ruta_excel.read_bytes()) as origen, BytesIO() as destino:
        with ZipFile(origen) as entrada, ZipFile(destino, "w", ZIP_DEFLATED) as salida:
            for nombre in entrada.namelist():
                contenido = entrada.read(nombre)
                if nombre == "xl/worksheets/sheet2.xml":
                    contenido = re.sub(
                        rb'<dimension ref="[^"]+"', b'<dimension ref="A1:A1"', contenido
                    )
                salida.writestr(nombre, contenido)
        ruta_excel.write_bytes(destino.getvalue())

    assert tuple(
        cliente.fila for cliente in LectorClientesExcel(ruta_excel).leer_clientes("IOL")
    ) == (3, 5, 8)


def test_archivo_corrupto_no_reutiliza_instantanea_anterior(ruta_excel: Path) -> None:
    lector = LectorClientesExcel(ruta_excel)
    assert lector.leer_clientes("IOL")
    ruta_excel.write_bytes(b"no es un archivo ZIP")
    with pytest.raises(EstructuraError) as capturada:
        lector.leer_clientes("IOL")
    assert capturada.value.__cause__ is not None


def test_archivo_ausente_no_crea_archivos(tmp_path: Path) -> None:
    ruta = tmp_path / "nueva" / "clientes.xlsx"
    with pytest.raises(PersistenciaError) as capturada:
        LectorClientesExcel(ruta).listar_hojas()
    assert isinstance(capturada.value.__cause__, FileNotFoundError)
    assert not ruta.parent.exists()


def test_acceso_denegado_se_traduce_sin_exponer_rutas(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    causa = PermissionError("Ruta privada")
    mocker.patch.object(Path, "read_bytes", side_effect=causa)
    with pytest.raises(PersistenciaError) as capturada:
        LectorClientesExcel(tmp_path / "clientes.xlsx").revision()
    assert capturada.value.__cause__ is causa
    assert "privada" not in str(capturada.value)


@pytest.mark.parametrize("extension", ["xls", "xlsm", "csv"])
def test_extensiones_no_admitidas_se_rechazan_sin_abrir(tmp_path: Path, extension: str) -> None:
    with pytest.raises(ConfiguracionError):
        LectorClientesExcel(tmp_path / f"clientes.{extension}")


def test_cierre_es_idempotente_y_bloquea_todas_las_lecturas(ruta_excel: Path) -> None:
    lector = LectorClientesExcel(ruta_excel)
    lector.listar_hojas()
    lector.cerrar()
    lector.cerrar()
    operaciones: tuple[Callable[[], object], ...] = (
        lector.listar_hojas,
        lector.revision,
        lector.__enter__,
        lambda: lector.leer_clientes("IOL"),
        lambda: lector.ultima_semana("IOL"),
        lambda: lector.leer_marcas("IOL", 4),
    )
    for operacion in operaciones:
        with pytest.raises(ConfiguracionError, match="cerrado"):
            operacion()


def test_contexto_cierra_sin_ocultar_excepcion(ruta_excel: Path) -> None:
    lector = LectorClientesExcel(ruta_excel)
    with pytest.raises(RuntimeError, match="prueba"), lector:
        lector.listar_hojas()
        raise RuntimeError("prueba")
    with pytest.raises(ConfiguracionError):
        lector.revision()
