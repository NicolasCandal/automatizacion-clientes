"""Garantías de compatibilidad sin implementaciones antiguas ni acceso real a Google."""

import importlib
import subprocess
import sys
from collections.abc import Iterator
from os import environ
from pathlib import Path

import pytest
from openpyxl import Workbook
from openpyxl.worksheet.worksheet import Worksheet
from pytest_mock import MockerFixture

from automatizacion_clientes.config import Config
from automatizacion_clientes.core.errores import (
    AutenticacionError,
    ConcurrenciaError,
    ConfiguracionError,
    EstructuraError,
    ServicioExternoError,
    ValidacionError,
)
from automatizacion_clientes.infra.formatos_compatibilidad import (
    alias_desde_historico,
    catalogo_para_coincidencias,
    contacto_desde_historico,
)
from matching.alias_store import AliasStore
from matching.lista_negra import ListaNegra
from matching.motor import construir_indice, resolver_contacto, resolver_semana
from repositorio import auth
from repositorio.repo_sheets import RepoSheets
from repositorio.repo_xlsx import RepoXlsx


@pytest.fixture(autouse=True)
def aislar_entorno(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for nombre in tuple(environ):
        if nombre.upper().startswith("AC_"):
            monkeypatch.delenv(nombre)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def libro(tmp_path: Path) -> Iterator[RepoXlsx]:
    ruta = tmp_path / "prueba.xlsx"
    documento = Workbook()
    inicial = documento.active
    assert isinstance(inicial, Worksheet)
    documento.remove(inicial)
    for nombre in ("Inviu", "IOL", "Bull", "Balanz", "PPI"):
        hoja = documento.create_sheet(nombre)
        hoja.cell(1, 1, "Cliente")
        hoja.cell(2, 4 if nombre == "Balanz" else 3, "Semana")
        hoja.cell(3, 1, "Axa Prueba")
    documento.save(ruta)
    documento.close()
    with RepoXlsx(ruta) as repositorio:
        yield repositorio


@pytest.mark.parametrize("paquete", ["indice", "matching", "repositorio"])
@pytest.mark.parametrize("ayuda", [False, True])
def test_comandos_retirados_no_abren_entradas_ni_exponen_argumentos(
    tmp_path: Path,
    paquete: str,
    ayuda: bool,
) -> None:
    (tmp_path / ".env").write_text("AC_LOG_LEVEL=valor_invalido\n", encoding="utf-8")
    opciones = ["--help"] if ayuda else ["resolver", "--out", "ruta_secreta.json"]
    proceso = subprocess.run(
        [sys.executable, "-m", paquete, *opciones],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert proceso.returncode == (0 if ayuda else 2)
    assert proceso.stdout == ""
    assert "automatizacion-clientes --help" in proceso.stderr
    assert "ruta_secreta" not in proceso.stderr
    assert not (tmp_path / "ruta_secreta.json").exists()


def test_importar_configuracion_anterior_no_carga_dotenv(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "AC_LOG_LEVEL=DEBUG\nVARIABLE_NO_CARGADA=secreto\n", encoding="utf-8"
    )
    modulo = importlib.import_module("repositorio.config")
    importlib.reload(modulo)
    assert "VARIABLE_NO_CARGADA" not in environ
    assert modulo.Config is Config
    assert modulo.cargar_config(ruta_env=None).log_level == "INFO"


def test_autenticacion_anterior_no_inicia_consentimiento(
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    autorizar = mocker.patch("webbrowser.open", side_effect=AssertionError("Navegador inesperado"))
    with pytest.raises(AutenticacionError):
        auth.obtener_credenciales(tmp_path / "cliente.json", tmp_path / "token.json")
    autorizar.assert_not_called()
    assert list(tmp_path.iterdir()) == []


def test_autenticacion_anterior_delega_sin_guardar_token(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    proveedor = mocker.patch.object(auth, "ProveedorCredencialesGoogle")
    auth.obtener_credenciales(tmp_path / "cliente.json", tmp_path / "token.json")
    proveedor.return_value.cargar.assert_called_once_with(persistir=False)
    proveedor.return_value.autorizar.assert_not_called()
    configuracion = proveedor.call_args.args[0]
    assert configuracion.credentials_path == tmp_path / "cliente.json"
    assert configuracion.google_alcances == (
        "https://www.googleapis.com/auth/spreadsheets.readonly",
    )


def test_escritura_anterior_sin_revision_no_modifica_libro(libro: RepoXlsx) -> None:
    original = libro.ruta.read_bytes()
    with pytest.raises(ConfiguracionError):
        libro.aplicar_marcas("IOL", 3, "Semana", {3})
    assert libro.ruta.read_bytes() == original


def test_revision_antigua_se_rechaza_aunque_se_use_la_fachada(libro: RepoXlsx) -> None:
    revision = libro.revision()
    libro.aplicar_marcas("IOL", 3, "Semana", {3}, revision_esperada=revision)
    with pytest.raises(ConcurrenciaError):
        libro.aplicar_marcas("IOL", 3, "Semana", set(), revision_esperada=revision)
    assert libro.leer_marcas("IOL", 3) == {3}


def test_cierre_de_fachada_invalida_lectura_y_escritura(libro: RepoXlsx) -> None:
    libro.cerrar()
    libro.cerrar()
    with pytest.raises(ConfiguracionError):
        libro.obtener_escritor()
    with pytest.raises(ConfiguracionError):
        libro.revision()
    with pytest.raises(ConfiguracionError), libro:
        pass


def test_fachada_sheets_no_crea_escritor_para_leer_y_cierra_ambos(
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    modulo = importlib.import_module("repositorio.repo_sheets")
    lector = mocker.Mock()
    escritor = mocker.Mock()
    mocker.patch.object(modulo, "crear_lector_sheets", return_value=lector)
    crear = mocker.patch.object(modulo, "crear_escritor_sheets", return_value=escritor)
    configuracion = Config(
        directorio_base=tmp_path, spreadsheet_id="documento_ficticio", _env_file=None
    )
    with RepoSheets("documento_ficticio", mocker.Mock(), configuracion=configuracion) as repo:
        crear.assert_not_called()
        assert repo.obtener_escritor() is escritor
        assert repo.obtener_escritor() is escritor
        crear.assert_called_once()
    lector.cerrar.assert_called_once()
    crear.return_value.cerrar.assert_called_once()
    repo.cerrar()
    with pytest.raises(ConfiguracionError):
        repo.obtener_escritor()


def test_fachada_sheets_rechaza_documento_incompatible_antes_de_crear_sesion(
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    modulo = importlib.import_module("repositorio.repo_sheets")
    crear = mocker.patch.object(modulo, "crear_lector_sheets")
    configuracion = Config(directorio_base=tmp_path, spreadsheet_id="otro", _env_file=None)
    with pytest.raises(ConfiguracionError):
        RepoSheets("documento_ficticio", mocker.Mock(), configuracion=configuracion)
    crear.assert_not_called()


def test_aliases_historicos_delegan_conciliacion_y_no_comparten_diccionarios(
    tmp_path: Path,
) -> None:
    almacen = AliasStore(tmp_path / "aliases.json")
    assert len(almacen) == 0
    assert almacen.registrar("Axa", [{"persona_id": "persona_1", "hojas": ["IOL"]}], "+123") is None
    entrada = almacen.buscar_por_nombre("Axa")
    assert entrada is not None
    entrada["personas"].clear()
    assert almacen.buscar_por_nombre("Axa") != entrada
    assert len(almacen) == 1
    assert almacen.buscar_por_telefono("+123") is not None
    assert almacen.buscar_por_telefono(None) is None
    almacen.guardar()
    assert AliasStore(almacen.ruta).buscar_por_nombre("Axa") is not None


def test_interfaz_singular_no_elige_un_alias_de_telefono_compartido(tmp_path: Path) -> None:
    almacen = AliasStore(tmp_path / "aliases.json")
    almacen.registrar("Axa", [{"persona_id": "persona_1", "hojas": ["IOL"]}], "+123")
    almacen.registrar("Otra", [{"persona_id": "persona_2", "hojas": ["Bull"]}], "+123")
    with pytest.raises(ValidacionError):
        almacen.buscar_por_telefono("+123")


@pytest.mark.parametrize("entrada", [[], None, {"personas": "invalido"}, {"personas": [None]}])
def test_catalogo_historico_malformado_no_se_acepta(entrada: object) -> None:
    with pytest.raises(EstructuraError):
        catalogo_para_coincidencias(entrada)


@pytest.mark.parametrize(
    "entrada",
    [
        {"nombre_crudo": "Axa", "ultima_fecha": "20261102"},
        {"nombre_crudo": "Axa", "ultima_fecha": "secreto"},
        {"nombre_crudo": "Axa", "ultima_fecha": "2026-11-02", "es_grupo": "false"},
    ],
)
def test_contacto_historico_requiere_fecha_y_tipos_validos(entrada: object) -> None:
    with pytest.raises(EstructuraError) as error:
        contacto_desde_historico(entrada)
    assert "secreto" not in str(error.value)


def test_alias_historico_no_omite_hojas_duplicadas() -> None:
    with pytest.raises(EstructuraError):
        alias_desde_historico(
            "Axa", {"personas": [{"persona_id": "persona_1", "hojas": ["IOL", "IOL"]}]}
        )


def test_error_de_cierre_no_oculta_error_del_bloque(libro: RepoXlsx, mocker: MockerFixture) -> None:
    mocker.patch.object(libro.lector, "cerrar", side_effect=ServicioExternoError("ficticio"))
    with pytest.raises(RuntimeError, match="original"), libro:
        raise RuntimeError("original")


def test_conversion_historica_conserva_todas_las_filas_de_una_persona(tmp_path: Path) -> None:
    catalogo = {
        "personas": [
            {
                "id": "persona_1",
                "clave_orden": "axa prueba",
                "nombre_canonico": "Axa Prueba",
                "titulares": ["Axa Prueba"],
                "apariciones": [
                    {"hoja": "IOL", "fila": 3, "nombre_crudo": "Axa Prueba"},
                    {"hoja": "IOL", "fila": 8, "nombre_crudo": "Axa Prueba"},
                ],
            }
        ]
    }
    resultados = resolver_contacto(
        {"nombre_crudo": "Axa Prueba", "ultima_fecha": "2026-11-02"},
        construir_indice(catalogo),
        AliasStore(tmp_path / "aliases.json"),
        ListaNegra(tmp_path / "exclusiones.json"),
    )
    assert len(resultados) == 1
    assert resultados[0]["tipo"] == "resuelto"
    assert resultados[0]["ubicaciones"] == [
        {"hoja": "IOL", "fila": 3},
        {"hoja": "IOL", "fila": 8},
    ]
    assert resultados[0]["fila_por_hoja"] == {"IOL": 3}


def test_lote_historico_invalido_no_aprende_aliases_parciales(tmp_path: Path) -> None:
    catalogo = {
        "personas": [
            {
                "id": "persona_1",
                "clave_orden": "axa prueba",
                "nombre_canonico": "Axa Prueba",
                "titulares": ["Axa Prueba"],
                "apariciones": [{"hoja": "IOL", "fila": 3, "nombre_crudo": "Axa Prueba"}],
            }
        ]
    }
    aliases = AliasStore(tmp_path / "aliases.json")
    with pytest.raises(EstructuraError, match="fecha"):
        resolver_semana(
            [
                {"nombre_crudo": "Axa Prueba", "ultima_fecha": "2026-11-02"},
                {"nombre_crudo": "Otro", "ultima_fecha": "fecha_invalida"},
            ],
            catalogo,
            aliases,
            ListaNegra(tmp_path / "exclusiones.json"),
        )
    assert len(aliases) == 0
    assert not aliases.ruta.exists()
