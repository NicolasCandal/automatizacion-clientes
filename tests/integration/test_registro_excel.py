"""Registro completo sobre XLSX y fuentes JSON temporales, sin datos personales reales."""

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook
from openpyxl.styles import PatternFill
from openpyxl.worksheet.worksheet import Worksheet
from pytest_mock import MockerFixture

from automatizacion_clientes.core.errores import ConcurrenciaError, RegistroInterrumpidoError
from automatizacion_clientes.core.modelos import (
    CatalogoClientes,
    ResolucionSemanal,
    SemanaContactos,
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
from automatizacion_clientes.services.registro import ServicioRegistro


@dataclass
class Escenario:
    ruta: Path
    lector: LectorClientesExcel
    escritor: EscritorClientesExcel
    servicio_catalogo: ServicioCatalogo
    catalogo: CatalogoClientes
    semana: SemanaContactos
    resolucion: ResolucionSemanal

    def servicio(self) -> ServicioRegistro:
        return ServicioRegistro(
            lector_clientes=self.lector,
            escritor_clientes=self.escritor,
            identificador_origen="excel_sintetico",
        )


@pytest.fixture
def escenario(tmp_path: Path) -> Iterator[Escenario]:
    ruta = tmp_path / "clientes.xlsx"
    libro = Workbook()
    inicial = libro.active
    assert isinstance(inicial, Worksheet)
    libro.remove(inicial)
    for nombre in ("Inviu", "IOL", "Bull", "Balanz", "PPI"):
        hoja: Worksheet = libro.create_sheet(nombre)
        hoja.cell(1, 1, "Cliente")
        primera = 4 if nombre == "Balanz" else 3
        hoja.cell(2, primera, "02 al 06-11")
        hoja.cell(3, 1, "Axa Prueba")
        hoja.cell(5, 1, "Notuka Ejemplo")
        hoja.cell(3, 2, "=100+200")
        if nombre == "IOL":
            hoja.cell(8, 1, "AXA PRUEBA Y/O NOTUKA EJEMPLO")
            hoja.cell(5, primera).fill = PatternFill(patternType="solid", fgColor="FF76923C")
    try:
        libro.save(ruta)
    finally:
        libro.close()
    ruta_contactos = tmp_path / "semana.json"
    guardar_json(
        ruta_contactos,
        {
            "rango": {"desde": "2026-11-02", "hasta": "2026-11-06"},
            "contactos": [
                {"nombre_crudo": "Axa Prueba (IOL/Balanz)", "ultima_fecha": "2026-11-05"}
            ],
        },
    )
    catalogos = AlmacenCatalogoJson(tmp_path / "catalogo.json")
    contactos = LectorContactosJson(ruta_contactos)
    coincidencias = ServicioCoincidencias(
        lector_contactos=contactos,
        almacen_catalogo=catalogos,
        almacen_aliases=AlmacenAliasesJson(tmp_path / "aliases.json"),
        lista_exclusion=ListaExclusionJson(tmp_path / "exclusiones.json"),
        almacen_resoluciones=AlmacenResolucionesJson(tmp_path / "resolucion.json"),
        comparador=ComparadorRapidFuzz(),
    )
    with LectorClientesExcel(ruta) as lector:
        servicio_catalogo = ServicioCatalogo(
            lector_clientes=lector,
            almacen_catalogo=catalogos,
            reloj=RelojSistema(),
            identificador_origen="excel_sintetico",
        )
        catalogo = servicio_catalogo.ejecutar(simulacion=False)
        yield Escenario(
            ruta=ruta,
            lector=lector,
            escritor=EscritorClientesExcel(
                ruta, directorio_respaldos=tmp_path / "respaldos", reloj=RelojSistema()
            ),
            servicio_catalogo=servicio_catalogo,
            catalogo=catalogo,
            semana=contactos.leer(),
            resolucion=coincidencias.ejecutar(),
        )


def test_simulacion_y_registro_reemplazan_solo_semana_elegida_con_respaldo_exacto(
    escenario: Escenario,
    tmp_path: Path,
) -> None:
    original = escenario.ruta.read_bytes()
    servicio = escenario.servicio()
    plan = servicio.planificar(
        catalogo=escenario.catalogo,
        semana=escenario.semana,
        resolucion=escenario.resolucion,
        encabezado="02 al 06-11",
    )
    resultado_simulado = servicio.ejecutar(plan)
    assert resultado_simulado.simulacion
    assert escenario.ruta.read_bytes() == original
    assert not (tmp_path / "respaldos").exists()
    assert not (tmp_path / "aliases.json").exists()
    assert not (tmp_path / "resolucion.json").exists()
    resultado = servicio.ejecutar(plan, simulacion=False)
    assert resultado.hojas_aplicadas == ("IOL", "Balanz")
    assert isinstance(resultado.respaldo, Path)
    assert resultado.respaldo.read_bytes() == original
    assert escenario.lector.leer_marcas("IOL", 3) == frozenset({3, 8})
    assert escenario.lector.leer_marcas("Balanz", 4) == frozenset({3})
    assert escenario.lector.leer_marcas("Inviu", 3) == frozenset()
    assert resultado.revision_final == escenario.lector.revision()
    libro = load_workbook(escenario.ruta, data_only=False)
    try:
        for nombre in libro.sheetnames:
            hoja = libro[nombre]
            assert isinstance(hoja, Worksheet)
            assert hoja.cell(3, 2).value == "=100+200"
    finally:
        libro.close()
    with pytest.raises(ConcurrenciaError):
        servicio.ejecutar(plan, simulacion=False)
    catalogo_actual = escenario.servicio_catalogo.ejecutar(simulacion=False)
    assert catalogo_actual.personas == escenario.catalogo.personas
    nuevo_plan = servicio.planificar(
        catalogo=catalogo_actual,
        semana=escenario.semana,
        resolucion=escenario.resolucion,
        encabezado="02 al 06-11",
    )
    revision = escenario.lector.revision()
    repetido = servicio.ejecutar(nuevo_plan, simulacion=False)
    assert repetido.hojas_aplicadas == ()
    assert repetido.respaldo is None
    assert escenario.lector.revision() == revision
    assert len(list((tmp_path / "respaldos").iterdir())) == 1


def test_semana_nueva_se_crea_en_todas_las_hojas_y_no_toca_la_anterior(
    escenario: Escenario,
) -> None:
    anteriores = {
        hoja.nombre: escenario.lector.leer_marcas(hoja.nombre, hoja.ultima_col_semana or 3)
        for hoja in escenario.catalogo.hojas
    }
    servicio = escenario.servicio()
    plan = servicio.planificar(
        catalogo=escenario.catalogo,
        semana=escenario.semana,
        resolucion=escenario.resolucion,
        encabezado="2026-11-02 / 2026-11-06",
    )
    resultado = servicio.ejecutar(plan, simulacion=False)
    assert resultado.hojas_aplicadas == ("Inviu", "IOL", "Bull", "Balanz", "PPI")
    for hoja in escenario.catalogo.hojas:
        assert (
            escenario.lector.leer_marcas(hoja.nombre, hoja.ultima_col_semana or 3)
            == anteriores[hoja.nombre]
        )
    assert escenario.lector.ultima_semana("Balanz") == (5, "2026-11-02 / 2026-11-06")
    assert escenario.lector.leer_marcas("IOL", 4) == frozenset({3, 8})
    assert escenario.lector.leer_marcas("Balanz", 5) == frozenset({3})


def test_cambio_real_durante_una_escritura_detiene_registro_y_conserva_respaldo(
    escenario: Escenario,
    mocker: MockerFixture,
) -> None:
    original = escenario.ruta.read_bytes()
    servicio = escenario.servicio()
    plan = servicio.planificar(
        catalogo=escenario.catalogo,
        semana=escenario.semana,
        resolucion=escenario.resolucion,
        encabezado="02 al 06-11",
    )
    aplicar = escenario.escritor.aplicar_marcas

    def aplicar_y_cambiar(
        hoja: str, columna: int, encabezado: str, filas: frozenset[int], *, revision_esperada: str
    ) -> None:
        aplicar(hoja, columna, encabezado, filas, revision_esperada=revision_esperada)
        libro = load_workbook(escenario.ruta, data_only=False)
        try:
            destino = libro["Balanz"]
            assert isinstance(destino, Worksheet)
            destino.cell(3, 1, "Cliente modificado externamente")
            libro.save(escenario.ruta)
        finally:
            libro.close()

    escritura = mocker.patch.object(
        escenario.escritor, "aplicar_marcas", side_effect=aplicar_y_cambiar
    )
    with pytest.raises(RegistroInterrumpidoError) as error:
        servicio.ejecutar(plan, simulacion=False)
    assert isinstance(error.value.__cause__, ConcurrenciaError)
    assert isinstance(error.value.respaldo, Path)
    assert error.value.respaldo.read_bytes() == original
    assert error.value.hojas_confirmadas == ()
    assert error.value.hoja_en_curso == "IOL"
    assert escritura.call_count == 1
    assert (
        escenario.lector.leer_clientes("Balanz")[0].nombre_crudo
        == "Cliente modificado externamente"
    )
    assert escenario.lector.leer_marcas("Balanz", 4) == frozenset()
