"""Comando completo sobre entradas sintéticas y directorios temporales."""

import subprocess
import sys
from dataclasses import replace
from os import environ
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook
from openpyxl.worksheet.worksheet import Worksheet
from pytest_mock import MockerFixture

from automatizacion_clientes import main as entrada
from automatizacion_clientes.config import Config
from automatizacion_clientes.core.errores import PersistenciaError
from automatizacion_clientes.core.modelos import (
    CatalogoClientes,
    ResolucionSemanal,
    SemanaContactos,
)
from automatizacion_clientes.infra.almacen_aliases_json import AlmacenAliasesJson
from automatizacion_clientes.infra.almacen_catalogo_json import AlmacenCatalogoJson
from automatizacion_clientes.infra.almacen_resoluciones_json import AlmacenResolucionesJson
from automatizacion_clientes.infra.almacenamiento_json import guardar_json
from automatizacion_clientes.infra.lector_clientes_excel import LectorClientesExcel
from automatizacion_clientes.infra.lector_contactos_json import LectorContactosJson
from automatizacion_clientes.services.coincidencias import ServicioCoincidencias


@pytest.fixture(autouse=True)
def aislar_entorno(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for nombre in tuple(environ):
        if nombre.upper().startswith("AC_"):
            monkeypatch.delenv(nombre)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def configuracion(tmp_path: Path) -> Config:
    configuracion = Config(directorio_base=tmp_path, _env_file=None)
    configuracion.excel_path.parent.mkdir(parents=True)
    libro = Workbook()
    inicial = libro.active
    assert isinstance(inicial, Worksheet)
    libro.remove(inicial)
    for nombre in ("Inviu", "IOL", "Bull", "Balanz", "PPI"):
        hoja: Worksheet = libro.create_sheet(nombre)
        hoja.cell(1, 1, "Cliente")
        hoja.cell(2, 4 if nombre == "Balanz" else 3, "Anterior")
        hoja.cell(3, 1, "Axa Prueba")
        hoja.cell(5, 1, "Notuka Ejemplo")
        hoja.cell(3, 2, "=100+200")
        if nombre == "IOL":
            hoja.cell(8, 1, "AXA PRUEBA Y/O NOTUKA EJEMPLO")
    try:
        libro.save(configuracion.excel_path)
    finally:
        libro.close()
    escribir_contactos(configuracion, "Axa Prueba (IOL/Balanz)")
    return configuracion


def escribir_contactos(configuracion: Config, nombre: str) -> None:
    guardar_json(
        configuracion.contactos_path,
        {
            "rango": {"desde": "2026-11-02", "hasta": "2026-11-06"},
            "contactos": [{"nombre_crudo": nombre, "ultima_fecha": "2026-11-05"}],
        },
    )


def archivos(base: Path) -> dict[Path, bytes]:
    return {ruta.relative_to(base): ruta.read_bytes() for ruta in base.rglob("*") if ruta.is_file()}


def argumentos(configuracion: Config) -> list[str]:
    return [
        "--sin-env",
        "--directorio-base",
        str(configuracion.directorio_base),
        "--encabezado",
        "2026-11-02 / 2026-11-06",
    ]


def test_simulacion_predeterminada_no_crea_ni_modifica_archivos(
    configuracion: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    originales = archivos(configuracion.directorio_base)
    assert entrada.main(argumentos(configuracion)) == 0
    assert archivos(configuracion.directorio_base) == originales
    salida = capsys.readouterr()
    assert salida.out == ""
    assert "Plan de hoja: IOL; columna=4" in salida.err
    assert "marcar=2" in salida.err
    assert "simulación=True" in salida.err
    assert "Axa Prueba" not in salida.err
    assert str(configuracion.excel_path) not in salida.err


def test_aplicar_guarda_informe_aliases_respaldo_y_marcas_y_admite_repeticion(
    configuracion: Config,
) -> None:
    original = configuracion.excel_path.read_bytes()
    assert entrada.main([*argumentos(configuracion), "--aplicar"]) == 0
    respaldos = list(configuracion.directorio_respaldos.iterdir())
    assert len(respaldos) == 1
    assert respaldos[0].read_bytes() == original
    assert AlmacenCatalogoJson(configuracion.catalogo_path).leer() is not None
    assert len(AlmacenResolucionesJson(configuracion.resolucion_path).leer().resueltos) == 1
    assert (
        AlmacenAliasesJson(configuracion.aliases_path).buscar_por_nombre("Axa Prueba (IOL/Balanz)")
        is not None
    )
    with LectorClientesExcel(configuracion.excel_path) as lector:
        assert lector.leer_marcas("IOL", 4) == frozenset({3, 8})
        assert lector.leer_marcas("Balanz", 5) == frozenset({3})
        assert lector.leer_marcas("Bull", 4) == frozenset()
    libro = load_workbook(configuracion.excel_path, data_only=False)
    try:
        hoja = libro["IOL"]
        assert isinstance(hoja, Worksheet)
        assert hoja.cell(3, 2).value == "=100+200"
    finally:
        libro.close()
    actual = configuracion.excel_path.read_bytes()
    assert entrada.main([*argumentos(configuracion), "--aplicar"]) == 0
    assert configuracion.excel_path.read_bytes() == actual
    assert len(list(configuracion.directorio_respaldos.iterdir())) == 1


@pytest.mark.parametrize("aplicar", [False, True])
def test_pendientes_devuelven_tres_sin_marcas_y_solo_guardan_informe_en_real(
    configuracion: Config, aplicar: bool
) -> None:
    escribir_contactos(configuracion, "Identidad inexistente zzz 987")
    original = configuracion.excel_path.read_bytes()
    opciones = ["--aplicar"] if aplicar else []
    assert entrada.main([*argumentos(configuracion), *opciones]) == 3
    assert configuracion.excel_path.read_bytes() == original
    assert not configuracion.directorio_respaldos.exists()
    assert not configuracion.aliases_path.exists()
    assert configuracion.catalogo_path.exists() is aplicar
    assert configuracion.resolucion_path.exists() is aplicar


def test_pendientes_se_pueden_incluir_de_forma_explicita(configuracion: Config) -> None:
    escribir_contactos(configuracion, "Identidad inexistente zzz 987")
    assert entrada.main([*argumentos(configuracion), "--aplicar", "--permitir-pendientes"]) == 0
    with LectorClientesExcel(configuracion.excel_path) as lector:
        assert lector.leer_marcas("IOL", 4) == frozenset()
    assert not configuracion.aliases_path.exists()


def test_simular_prevalece_sobre_la_configuracion_real(
    configuracion: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AC_SIMULACION", "false")
    originales = archivos(configuracion.directorio_base)
    assert entrada.main([*argumentos(configuracion), "--simular"]) == 0
    assert archivos(configuracion.directorio_base) == originales


def test_modo_real_desde_entorno_se_respeta(
    configuracion: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AC_SIMULACION", "false")
    assert entrada.main(argumentos(configuracion)) == 0
    assert configuracion.resolucion_path.exists()
    assert configuracion.directorio_respaldos.exists()


def test_cache_invalida_solo_se_reconstruye_con_opcion_explicita(configuracion: Config) -> None:
    configuracion.catalogo_path.parent.mkdir(parents=True)
    configuracion.catalogo_path.write_bytes(b"{invalido")
    original = archivos(configuracion.directorio_base)
    assert entrada.main(argumentos(configuracion)) == 2
    assert archivos(configuracion.directorio_base) == original
    assert entrada.main([*argumentos(configuracion), "--reconstruir-catalogo"]) == 0
    assert archivos(configuracion.directorio_base) == original
    assert entrada.main([*argumentos(configuracion), "--reconstruir-catalogo", "--aplicar"]) == 0
    assert AlmacenCatalogoJson(configuracion.catalogo_path).leer() is not None


def test_error_de_configuracion_no_expone_valores(
    configuracion: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AC_LOG_LEVEL", "secreto")
    assert entrada.main(argumentos(configuracion)) == 2
    assert "secreto" not in capsys.readouterr().err


def test_cierra_lector_incluso_si_falla_contactos(
    configuracion: Config, mocker: MockerFixture
) -> None:
    configuracion.contactos_path.write_bytes(b"{invalido")
    cierre = mocker.spy(LectorClientesExcel, "cerrar")
    assert entrada.main(argumentos(configuracion)) == 2
    cierre.assert_called_once()


def test_error_de_informe_impide_escritura_excel(
    configuracion: Config, mocker: MockerFixture
) -> None:
    original = configuracion.excel_path.read_bytes()
    mocker.patch.object(
        AlmacenResolucionesJson, "guardar", side_effect=PersistenciaError("Fallo simulado.")
    )
    assert entrada.main([*argumentos(configuracion), "--aplicar"]) == 4
    assert configuracion.excel_path.read_bytes() == original
    assert not configuracion.directorio_respaldos.exists()


def test_desacuerdo_entre_previsualizacion_y_resolucion_impide_marcas(
    configuracion: Config, mocker: MockerFixture
) -> None:
    original = configuracion.excel_path.read_bytes()
    resolver = ServicioCoincidencias.ejecutar

    def resolver_con_cambio(
        self: ServicioCoincidencias,
        *,
        simulacion: bool = True,
        catalogo: CatalogoClientes | None = None,
        semana: SemanaContactos | None = None,
    ) -> ResolucionSemanal:
        resultado = resolver(self, simulacion=simulacion, catalogo=catalogo, semana=semana)
        return resultado if simulacion else replace(resultado, avisos=("Cambio simulado.",))

    mocker.patch.object(
        ServicioCoincidencias, "ejecutar", autospec=True, side_effect=resolver_con_cambio
    )
    assert entrada.main([*argumentos(configuracion), "--aplicar"]) == 5
    assert configuracion.excel_path.read_bytes() == original
    assert not configuracion.directorio_respaldos.exists()


def test_contactos_se_leen_una_sola_vez_en_ejecucion_real(
    configuracion: Config, mocker: MockerFixture
) -> None:
    lectura = mocker.spy(LectorContactosJson, "leer")
    assert entrada.main([*argumentos(configuracion), "--aplicar"]) == 0
    lectura.assert_called_once()


def test_modulo_ejecutable_ofrece_ayuda_sin_leer_env_ni_crear_archivos(tmp_path: Path) -> None:
    proceso = subprocess.run(
        [sys.executable, "-m", "automatizacion_clientes", "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert proceso.returncode == 0
    assert proceso.stdout == ""
    assert "--aplicar" in proceso.stderr
    assert list(tmp_path.iterdir()) == []
