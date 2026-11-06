from pathlib import Path

from openpyxl import Workbook
from openpyxl.worksheet.worksheet import Worksheet

from indice.lector import construir_catalogo, contar_personas_multi_hoja
from repositorio.repo_xlsx import RepoXlsx

HOJAS = ["Inviu", "IOL", "Bull", "Balanz", "PPI"]


def _armar_excel_fixture(tmp_path: Path) -> Path:
    wb = Workbook()
    inicial = wb.active
    assert isinstance(inicial, Worksheet)
    wb.remove(inicial)

    clientes_comunes = ["Fedezu, Lidexe Bekaxo", "PEXEZA LEVOBA", "Goxane Fuzifa DELEVE"]
    for nombre_hoja in ["Inviu", "IOL", "Bull", "PPI"]:
        ws = wb.create_sheet(nombre_hoja)
        ws.cell(row=1, column=1, value="Cliente")
        ws.cell(row=1, column=2, value="Capital")
        ws.cell(row=2, column=3, value="15 al 19-12")
        ws.cell(row=2, column=4, value="22 al 26-12")
        for i, nombre in enumerate(clientes_comunes):
            fila = 3 + i
            ws.cell(row=fila, column=1, value=nombre)
            ws.cell(row=fila, column=2, value=1000 * (i + 1))

    ws = wb.create_sheet("Balanz")
    ws.cell(row=1, column=1, value="CLIENTES")
    ws.cell(row=1, column=2, value="Capital")
    ws.cell(row=1, column=3, value="Capital 2")
    ws.cell(row=2, column=4, value="15 al 19-12")
    ws.cell(row=3, column=1, value="Lidexe Bekaxo Fedezu")
    ws.cell(row=4, column=1, value="RAXOZI GOGOTE Y/O RETUBO GEZIGE RATIXU")
    ws.cell(row=5, column=1, value="MOPIVO BEMIBE (esposa Gosoru Fezixi)")

    ruta = tmp_path / "fixture.xlsx"
    wb.save(ruta)
    return ruta


def test_construir_catalogo_estructura_por_hoja(tmp_path: Path) -> None:
    repo = RepoXlsx(_armar_excel_fixture(tmp_path))
    catalogo = construir_catalogo(repo, "fixture")

    assert set(catalogo["hojas"].keys()) == set(HOJAS)
    assert catalogo["hojas"]["IOL"]["primera_col_semana"] == 3
    assert catalogo["hojas"]["IOL"]["ultima_col_semana"] == 4
    assert catalogo["hojas"]["IOL"]["filas_cliente"] == 3

    assert catalogo["hojas"]["Balanz"]["primera_col_semana"] == 4
    assert catalogo["hojas"]["Balanz"]["ultima_col_semana"] == 4
    assert catalogo["hojas"]["Balanz"]["filas_cliente"] == 3

    assert "revision" in catalogo
    assert catalogo["spreadsheet_id"] == "fixture"


def test_deduplicacion_entre_hojas(tmp_path: Path) -> None:
    repo = RepoXlsx(_armar_excel_fixture(tmp_path))
    catalogo = construir_catalogo(repo, "fixture")

    personas_fedezu = [
        p for p in catalogo["personas"] if p["clave_orden"] == "bekaxo fedezu lidexe"
    ]
    assert len(personas_fedezu) == 1
    hojas_fedezu = {a["hoja"] for a in personas_fedezu[0]["apariciones"]}
    assert hojas_fedezu == {"Inviu", "IOL", "Bull", "PPI", "Balanz"}

    assert contar_personas_multi_hoja(catalogo) >= 1


def test_titularidad_compartida_genera_personas_separadas_misma_fila(tmp_path: Path) -> None:
    repo = RepoXlsx(_armar_excel_fixture(tmp_path))
    catalogo = construir_catalogo(repo, "fixture")

    persona_gogote = next(
        p for p in catalogo["personas"] if p["nombre_canonico"] == "RAXOZI GOGOTE"
    )
    persona_gezige = next(
        p for p in catalogo["personas"] if p["nombre_canonico"] == "RETUBO GEZIGE RATIXU"
    )
    assert persona_gogote["apariciones"][0]["fila"] == persona_gezige["apariciones"][0]["fila"]
    assert persona_gogote["apariciones"][0]["hoja"] == "Balanz"


def test_titularidad_parentesis_esposa_como_persona_extra(tmp_path: Path) -> None:
    repo = RepoXlsx(_armar_excel_fixture(tmp_path))
    catalogo = construir_catalogo(repo, "fixture")

    persona_gosoru = next(
        p for p in catalogo["personas"] if p["nombre_canonico"] == "Gosoru Fezixi"
    )
    assert (
        persona_gosoru["apariciones"][0]["nombre_crudo"] == "MOPIVO BEMIBE (esposa Gosoru Fezixi)"
    )


def test_revision_estable_si_archivo_no_cambia(tmp_path: Path) -> None:
    ruta = _armar_excel_fixture(tmp_path)
    repo1 = RepoXlsx(ruta)
    repo2 = RepoXlsx(ruta)
    assert (
        construir_catalogo(repo1, "fixture")["revision"]
        == construir_catalogo(repo2, "fixture")["revision"]
    )
