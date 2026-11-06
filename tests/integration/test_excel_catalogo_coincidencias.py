"""Recorrido real desde XLSX hasta catálogo, informe y aliases JSON."""

from datetime import date
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook
from openpyxl.worksheet.worksheet import Worksheet
from pytest_mock import MockerFixture

from automatizacion_clientes.core.coincidencia import MotorCoincidencias
from automatizacion_clientes.core.errores import ConcurrenciaError
from automatizacion_clientes.core.modelos import (
    ContactoResuelto,
    ContactoSemanal,
    FilaCliente,
    UbicacionCliente,
)
from automatizacion_clientes.infra.almacen_aliases_json import AlmacenAliasesJson
from automatizacion_clientes.infra.almacen_catalogo_json import AlmacenCatalogoJson
from automatizacion_clientes.infra.almacen_resoluciones_json import AlmacenResolucionesJson
from automatizacion_clientes.infra.almacenamiento_json import guardar_json
from automatizacion_clientes.infra.comparador_rapidfuzz import ComparadorRapidFuzz
from automatizacion_clientes.infra.escritor_clientes_excel import EscritorClientesExcel
from automatizacion_clientes.infra.lector_clientes_excel import LectorClientesExcel
from automatizacion_clientes.infra.lector_contactos_json import LectorContactosJson
from automatizacion_clientes.infra.lista_exclusion_json import ListaExclusionJson
from automatizacion_clientes.infra.reloj_sistema import RelojSistema
from automatizacion_clientes.services.catalogo import ServicioCatalogo
from automatizacion_clientes.services.coincidencias import ServicioCoincidencias


@pytest.fixture
def ruta_excel(tmp_path: Path) -> Path:
    libro = Workbook()
    inicial = libro.active
    assert isinstance(inicial, Worksheet)
    libro.remove(inicial)
    for nombre in ("Inviu", "IOL", "Bull", "Balanz", "PPI"):
        hoja: Worksheet = libro.create_sheet(nombre)
        hoja.cell(1, 1, "Cliente")
        hoja.cell(2, 4 if nombre == "Balanz" else 3, "02 al 06-11")
        hoja.cell(3, 1, "Axa Prueba")
        if nombre == "IOL":
            hoja.cell(5, 1, "Prueba, Axa")
            hoja.cell(8, 1, "Notuka Ejemplo")
            hoja.cell(3, 2, "=100+200")
    ruta = tmp_path / "clientes.xlsx"
    try:
        libro.save(ruta)
    finally:
        libro.close()
    return ruta


def test_excel_alimenta_catalogo_y_coincidencias_sin_modificar_origen(
    tmp_path: Path, ruta_excel: Path
) -> None:
    original = ruta_excel.read_bytes()
    catalogos = AlmacenCatalogoJson(tmp_path / "catalogo.json")
    with LectorClientesExcel(ruta_excel) as lector:
        servicio_catalogo = ServicioCatalogo(
            lector_clientes=lector,
            almacen_catalogo=catalogos,
            reloj=RelojSistema(),
            identificador_origen="excel_sintetico",
        )
        simulado = servicio_catalogo.ejecutar()
        assert not (tmp_path / "catalogo.json").exists()
        catalogo = servicio_catalogo.ejecutar(simulacion=False)
    assert catalogo.personas == simulado.personas
    assert len(catalogo.personas) == 2
    ruta_contactos = tmp_path / "semana.json"
    guardar_json(
        ruta_contactos,
        {
            "rango": {"desde": "2026-11-02", "hasta": "2026-11-06"},
            "contactos": [{"nombre_crudo": "Axa Prueba (IOL/Bull)", "ultima_fecha": "2026-11-05"}],
        },
    )
    aliases = AlmacenAliasesJson(tmp_path / "aliases.json")
    informes = AlmacenResolucionesJson(tmp_path / "informe.json")
    servicio = ServicioCoincidencias(
        lector_contactos=LectorContactosJson(ruta_contactos),
        almacen_catalogo=catalogos,
        almacen_aliases=aliases,
        lista_exclusion=ListaExclusionJson(tmp_path / "exclusiones.json"),
        almacen_resoluciones=informes,
        comparador=ComparadorRapidFuzz(),
    )

    resolucion = servicio.ejecutar(simulacion=False)

    assert len(resolucion.resueltos) == 1
    assert resolucion.resueltos[0].ubicaciones == (
        UbicacionCliente(hoja="IOL", fila=3),
        UbicacionCliente(hoja="IOL", fila=5),
        UbicacionCliente(hoja="Bull", fila=3),
    )
    assert informes.leer() == resolucion
    assert (
        AlmacenAliasesJson(tmp_path / "aliases.json").buscar_por_nombre("Axa Prueba (IOL/Bull)")
        is not None
    )
    assert ruta_excel.read_bytes() == original


def test_cambio_real_del_excel_durante_lectura_no_sustituye_catalogo(
    tmp_path: Path, ruta_excel: Path, mocker: MockerFixture
) -> None:
    ruta_catalogo = tmp_path / "catalogo.json"
    with LectorClientesExcel(ruta_excel) as lector:
        servicio = ServicioCatalogo(
            lector_clientes=lector,
            almacen_catalogo=AlmacenCatalogoJson(ruta_catalogo),
            reloj=RelojSistema(),
            identificador_origen="excel_sintetico",
        )
        servicio.ejecutar(simulacion=False)
        cache_previa = ruta_catalogo.read_bytes()
        lectura_original = lector.leer_clientes

        def leer_con_cambio(nombre: str) -> tuple[FilaCliente, ...]:
            clientes = lectura_original(nombre)
            if nombre == "IOL":
                libro = load_workbook(ruta_excel, data_only=False)
                try:
                    hoja = libro["IOL"]
                    assert isinstance(hoja, Worksheet)
                    hoja.cell(8, 1, "Cliente incorporado durante la lectura")
                    libro.save(ruta_excel)
                finally:
                    libro.close()
            return clientes

        mocker.patch.object(lector, "leer_clientes", side_effect=leer_con_cambio)

        with pytest.raises(ConcurrenciaError):
            servicio.ejecutar(simulacion=False, forzar=True)

    assert ruta_catalogo.read_bytes() == cache_previa


def test_respaldo_y_marcas_de_todas_las_apariciones_refrescan_catalogo(
    tmp_path: Path,
    ruta_excel: Path,
) -> None:
    original = ruta_excel.read_bytes()
    escritor = EscritorClientesExcel(
        ruta_excel,
        directorio_respaldos=tmp_path / "respaldos",
        reloj=RelojSistema(),
    )
    with LectorClientesExcel(ruta_excel) as lector:
        servicio = ServicioCatalogo(
            lector_clientes=lector,
            almacen_catalogo=AlmacenCatalogoJson(tmp_path / "catalogo.json"),
            reloj=RelojSistema(),
            identificador_origen="excel_sintetico",
        )
        catalogo = servicio.ejecutar(simulacion=False)
        motor = MotorCoincidencias(catalogo, ComparadorRapidFuzz())
        resultados = motor.resolver_contacto(
            ContactoSemanal(
                nombre_crudo="Axa Prueba (IOL/Bull)",
                ultima_fecha=date(2026, 11, 5),
            )
        )
        assert len(resultados) == 1
        resuelto = resultados[0]
        assert isinstance(resuelto, ContactoResuelto)
        respaldo = escritor.crear_respaldo()
        filas_iol = frozenset(
            ubicacion.fila for ubicacion in resuelto.ubicaciones if ubicacion.hoja == "IOL"
        )
        escritor.aplicar_marcas(
            "IOL", 3, "02 al 06-11", filas_iol, revision_esperada=catalogo.revision
        )
        assert lector.leer_marcas("IOL", 3) == frozenset({3, 5})
        with pytest.raises(ConcurrenciaError):
            escritor.aplicar_marcas(
                "Bull", 3, "02 al 06-11", frozenset({3}), revision_esperada=catalogo.revision
            )
        escritor.aplicar_marcas(
            "Bull", 3, "02 al 06-11", frozenset({3}), revision_esperada=lector.revision()
        )
        assert lector.leer_marcas("Bull", 3) == frozenset({3})
        nuevo_catalogo = servicio.ejecutar(simulacion=False)
        assert nuevo_catalogo.revision != catalogo.revision
        assert nuevo_catalogo.personas == catalogo.personas
    assert respaldo.read_bytes() == original
    libro = load_workbook(ruta_excel, data_only=False)
    try:
        hoja = libro["IOL"]
        assert isinstance(hoja, Worksheet)
        assert hoja.cell(3, 2).value == "=100+200"
    finally:
        libro.close()
