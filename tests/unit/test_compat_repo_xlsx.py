from pathlib import Path

import pytest
from openpyxl import Workbook
from openpyxl.worksheet.worksheet import Worksheet

from repositorio.repo_xlsx import RepoXlsx
from repositorio.tipos import ErrorEstructura

HOJAS = ["Inviu", "IOL", "Bull", "Balanz", "PPI"]


def _armar_excel_fixture(tmp_path: Path) -> Path:
    wb = Workbook()
    inicial = wb.active
    assert isinstance(inicial, Worksheet)
    wb.remove(inicial)

    for nombre_hoja in ["Inviu", "IOL", "Bull", "PPI"]:
        ws = wb.create_sheet(nombre_hoja)
        ws.cell(row=1, column=1, value="Cliente")
        ws.cell(row=1, column=2, value="Capital")
        ws.cell(row=2, column=3, value="15 al 19-12")
        for i, nombre in enumerate(["Sivivu, Mizote", "Kukeve, Radono", "Gibelo, Fexira"]):
            fila = 3 + i
            ws.cell(row=fila, column=1, value=nombre)
            ws.cell(row=fila, column=2, value=1000 * (i + 1))

    ws = wb.create_sheet("Balanz")
    ws.cell(row=1, column=1, value="CLIENTES")
    ws.cell(row=2, column=4, value="15 al 19-12")
    ws.cell(row=3, column=1, value="Voledi, Axa")

    ruta = tmp_path / "fixture.xlsx"
    wb.save(ruta)
    return ruta


def test_listar_hojas(tmp_path: Path) -> None:
    repo = RepoXlsx(_armar_excel_fixture(tmp_path))
    hojas = {h.nombre: h for h in repo.listar_hojas()}
    assert set(hojas) == set(HOJAS)
    assert hojas["IOL"].primera_col_semana == 3
    assert hojas["IOL"].ultima_col_semana == 3
    assert hojas["IOL"].ultima_fila_con_cliente == 5
    assert hojas["Balanz"].primera_col_semana == 4
    assert hojas["Balanz"].ultima_fila_con_cliente == 3


def test_leer_clientes(tmp_path: Path) -> None:
    repo = RepoXlsx(_armar_excel_fixture(tmp_path))
    clientes = repo.leer_clientes("IOL")
    assert [c.nombre_crudo for c in clientes] == [
        "Sivivu, Mizote",
        "Kukeve, Radono",
        "Gibelo, Fexira",
    ]


def test_falta_hoja_lanza_error(tmp_path: Path) -> None:
    wb = Workbook()
    inicial = wb.active
    assert isinstance(inicial, Worksheet)
    wb.remove(inicial)
    for nombre_hoja in ["Inviu", "IOL", "Bull", "Balanz"]:  # falta PPI
        ws = wb.create_sheet(nombre_hoja)
        ws.cell(row=1, column=1, value="Cliente")
    ruta = tmp_path / "incompleto.xlsx"
    wb.save(ruta)

    with pytest.raises(ErrorEstructura):
        RepoXlsx(ruta).listar_hojas()


def test_aplicar_marcas_pinta_verde_y_blanco(tmp_path: Path) -> None:
    ruta = _armar_excel_fixture(tmp_path)
    repo = RepoXlsx(ruta)

    repo.aplicar_marcas(
        "IOL", col=4, encabezado="22 al 26-12", filas={3, 5}, revision_esperada=repo.revision()
    )

    marcadas = repo.leer_marcas("IOL", col=4)
    assert marcadas == {3, 5}

    col, encabezado = repo.ultima_semana("IOL")
    assert col == 4
    assert encabezado == "22 al 26-12"


def test_aplicar_marcas_es_idempotente(tmp_path: Path) -> None:
    ruta = _armar_excel_fixture(tmp_path)
    repo = RepoXlsx(ruta)

    repo.aplicar_marcas(
        "IOL", col=4, encabezado="22 al 26-12", filas={3}, revision_esperada=repo.revision()
    )
    repo.aplicar_marcas(
        "IOL", col=4, encabezado="22 al 26-12", filas={3, 4}, revision_esperada=repo.revision()
    )
    repo.aplicar_marcas(
        "IOL", col=4, encabezado="22 al 26-12", filas={3, 4}, revision_esperada=repo.revision()
    )

    hojas = {h.nombre: h for h in repo.listar_hojas()}
    # Solo debe existir UNA columna nueva (la 4), no una por cada corrida.
    assert hojas["IOL"].ultima_col_semana == 4
    assert repo.leer_marcas("IOL", col=4) == {3, 4}


def test_crear_respaldo_no_modifica_original(tmp_path: Path) -> None:
    ruta = _armar_excel_fixture(tmp_path)
    repo = RepoXlsx(ruta)
    original = ruta.read_bytes()

    ruta_respaldo = repo.crear_respaldo()

    assert ruta.read_bytes() == original
    from pathlib import Path

    assert Path(ruta_respaldo).exists()
    assert Path(ruta_respaldo).read_bytes() == original


def test_revision_cambia_al_escribir(tmp_path: Path) -> None:
    ruta = _armar_excel_fixture(tmp_path)
    repo = RepoXlsx(ruta)
    antes = repo.revision()

    repo.aplicar_marcas(
        "IOL", col=4, encabezado="22 al 26-12", filas={3}, revision_esperada=repo.revision()
    )

    assert repo.revision() != antes
