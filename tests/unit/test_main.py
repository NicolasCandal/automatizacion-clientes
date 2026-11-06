"""Argumentos, códigos de salida y logging de consola sin datos reales."""

import logging
import re
from os import environ
from pathlib import Path

import pytest
from pydantic_settings import SettingsError
from pytest_mock import MockerFixture

from automatizacion_clientes import main as entrada
from automatizacion_clientes.config import Config
from automatizacion_clientes.core.errores import (
    AutenticacionError,
    AutomatizacionError,
    ConcurrenciaError,
    ConfiguracionError,
    EstructuraError,
    PersistenciaError,
    RegistroInterrumpidoError,
)
from automatizacion_clientes.infra.logging_consola import logging_consola


@pytest.fixture(autouse=True)
def aislar_entorno(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for nombre in tuple(environ):
        if nombre.upper().startswith("AC_"):
            monkeypatch.delenv(nombre)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def configuracion(tmp_path: Path, mocker: MockerFixture) -> Config:
    configuracion = Config(directorio_base=tmp_path, _env_file=None)
    mocker.patch.object(entrada, "cargar_config", return_value=configuracion)
    return configuracion


def test_ayuda_no_carga_configuracion_ni_abre_entradas(
    mocker: MockerFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    carga = mocker.patch.object(entrada, "cargar_config")
    ejecutar = mocker.patch.object(entrada, "ejecutar_excel")
    assert entrada.main(["--help"]) == 0
    carga.assert_not_called()
    ejecutar.assert_not_called()
    salida = capsys.readouterr()
    assert salida.out == ""
    assert "--simular" in salida.err
    assert "--aplicar" in salida.err
    assert re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z INFO", salida.err)


@pytest.mark.parametrize(
    "argumentos",
    [
        [],
        ["--encabezado", " "],
        ["--desconocido", "secreto"],
        ["--encabezado"],
        ["--encabezado", "Semana", "--simular", "--aplicar"],
        ["--sin-env", "--env-file", "secreto"],
    ],
)
def test_argumentos_invalidos_no_exponen_sus_valores(
    argumentos: list[str], mocker: MockerFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    carga = mocker.patch.object(entrada, "cargar_config")
    assert entrada.main(argumentos) == 2
    carga.assert_not_called()
    salida = capsys.readouterr()
    assert salida.out == ""
    assert "secreto" not in salida.err
    assert "ERROR" in salida.err


@pytest.mark.parametrize(
    ("opcion", "esperada"), [((), None), (("--simular",), True), (("--aplicar",), False)]
)
def test_opciones_de_modo_y_encabezado_se_transmiten(
    configuracion: Config, mocker: MockerFixture, opcion: tuple[str, ...], esperada: bool | None
) -> None:
    resultado = mocker.Mock(simulacion=True, registro=mocker.Mock(hojas_aplicadas=()))
    ejecutar = mocker.patch.object(entrada, "ejecutar_excel", return_value=resultado)
    assert entrada.main(["--sin-env", "--encabezado", " Semana ", *opcion]) == 0
    ejecutar.assert_called_once_with(
        configuracion,
        encabezado="Semana",
        simulacion=esperada,
        permitir_pendientes=False,
        forzar_catalogo=False,
    )


def test_env_y_base_explicitos_se_resuelven_sin_exponer_rutas(
    configuracion: Config, mocker: MockerFixture
) -> None:
    (configuracion.directorio_base / "opciones.env").write_text("", encoding="utf-8")
    carga = mocker.patch.object(entrada, "cargar_config", return_value=configuracion)
    ejecutar = mocker.patch.object(
        entrada, "ejecutar_excel", return_value=mocker.Mock(registro=None)
    )
    assert (
        entrada.main(
            [
                "--env-file",
                "opciones.env",
                "--directorio-base",
                str(configuracion.directorio_base),
                "--encabezado",
                "Semana",
                "--permitir-pendientes",
                "--reconstruir-catalogo",
            ]
        )
        == 3
    )
    carga.assert_called_once_with(
        Path("opciones.env"), directorio_base=configuracion.directorio_base
    )
    assert ejecutar.call_args.kwargs["permitir_pendientes"]
    assert ejecutar.call_args.kwargs["forzar_catalogo"]


def test_env_file_explicito_ausente_no_utiliza_valores_por_defecto(
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    carga = mocker.patch.object(entrada, "cargar_config")
    assert (
        entrada.main(
            [
                "--env-file",
                "ausente.env",
                "--directorio-base",
                str(tmp_path),
                "--encabezado",
                "Semana",
                "--aplicar",
            ]
        )
        == 2
    )
    carga.assert_not_called()


@pytest.mark.parametrize(
    ("causa", "codigo"),
    [
        (ConfiguracionError("secreto"), 2),
        (EstructuraError("secreto"), 2),
        (ConcurrenciaError("secreto"), 5),
        (PersistenciaError("secreto"), 4),
        (OSError("secreto"), 4),
        (AutomatizacionError("secreto"), 1),
        (RuntimeError("secreto"), 1),
        (KeyboardInterrupt(), 130),
        (
            RegistroInterrumpidoError(
                respaldo=Path("ruta_secreta.xlsx"),
                hojas_confirmadas=("IOL",),
                hoja_en_curso="Balanz",
            ),
            6,
        ),
    ],
)
def test_codigos_y_mensajes_sanitizados(
    configuracion: Config,
    mocker: MockerFixture,
    capsys: pytest.CaptureFixture[str],
    causa: BaseException,
    codigo: int,
) -> None:
    mocker.patch.object(entrada, "ejecutar_excel", side_effect=causa)
    assert entrada.main(["--sin-env", "--encabezado", "Semana"]) == codigo
    salida = capsys.readouterr()
    assert salida.out == ""
    assert "secreto" not in salida.err
    assert "ruta_secreta" not in salida.err
    assert "Traceback" not in salida.err


def test_error_de_settings_no_exhibe_el_valor(
    mocker: MockerFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    mocker.patch.object(entrada, "cargar_config", side_effect=SettingsError("secreto"))
    assert entrada.main(["--sin-env", "--encabezado", "Semana"]) == 2
    assert "secreto" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "campo",
    ["catalogo_path", "aliases_path", "resolucion_path", "lista_negra_path", "contactos_path"],
)
def test_rutas_coincidentes_se_rechazan_antes_de_construir_adaptadores(
    configuracion: Config, mocker: MockerFixture, campo: str
) -> None:
    configuracion = configuracion.model_copy(update={campo: configuracion.excel_path})
    lector = mocker.patch.object(entrada, "LectorClientesExcel")
    with pytest.raises(ConfiguracionError):
        entrada.ejecutar_excel(configuracion, encabezado="Semana")
    lector.assert_not_called()


def test_rutas_anidadas_y_respaldo_sobre_un_archivo_se_rechazan(configuracion: Config) -> None:
    with pytest.raises(ConfiguracionError):
        entrada.validar_rutas(
            configuracion.model_copy(
                update={"catalogo_path": configuracion.aliases_path / "otra.json"}
            )
        )
    with pytest.raises(ConfiguracionError):
        entrada.validar_rutas(
            configuracion.model_copy(update={"directorio_respaldos": configuracion.excel_path})
        )
    with pytest.raises(ConfiguracionError):
        entrada.validar_rutas(configuracion, archivo_env=configuracion.catalogo_path)


def test_sheets_no_se_conecta_al_repositorio_anterior(
    configuracion: Config, mocker: MockerFixture
) -> None:
    lector = mocker.patch.object(entrada, "LectorClientesExcel")
    with pytest.raises(ConfiguracionError):
        entrada.ejecutar_excel(
            configuracion.model_copy(update={"repositorio": "sheets"}), encabezado="Semana"
        )
    lector.assert_not_called()


def test_logging_restaura_estado_y_no_modifica_raiz(capsys: pytest.CaptureFixture[str]) -> None:
    paquete = logging.getLogger("automatizacion_clientes")
    raiz = logging.getLogger()
    nivel, propagacion, manejadores = paquete.level, paquete.propagate, tuple(paquete.handlers)
    manejadores_raiz = tuple(raiz.handlers)
    for _ in range(2):
        with logging_consola() as consola:
            consola.info("Mensaje de prueba.")
    assert paquete.level == nivel
    assert paquete.propagate == propagacion
    assert tuple(paquete.handlers) == manejadores
    assert tuple(raiz.handlers) == manejadores_raiz
    assert capsys.readouterr().err.count("Mensaje de prueba.") == 2


def test_logging_se_restaura_incluso_si_falla_el_bloque() -> None:
    paquete = logging.getLogger("automatizacion_clientes")
    manejadores = tuple(paquete.handlers)
    with pytest.raises(RuntimeError), logging_consola():
        raise RuntimeError("Fallo de prueba.")
    assert tuple(paquete.handlers) == manejadores


@pytest.mark.parametrize(
    "opciones",
    [
        ["--encabezado", "Semana"],
        ["--simular"],
        ["--aplicar"],
        ["--permitir-pendientes"],
        ["--reconstruir-catalogo"],
    ],
)
def test_autorizacion_no_admite_opciones_de_registro(
    opciones: list[str],
    mocker: MockerFixture,
) -> None:
    carga = mocker.patch.object(entrada, "cargar_config")
    autorizar = mocker.patch.object(entrada, "autorizar_google")
    assert entrada.main(["--autorizar-google", *opciones]) == 2
    carga.assert_not_called()
    autorizar.assert_not_called()


def test_autorizacion_explicita_no_ejecuta_flujo(
    configuracion: Config,
    mocker: MockerFixture,
) -> None:
    configuracion = configuracion.model_copy(
        update={"repositorio": "sheets", "spreadsheet_id": "documento_sintetico"}
    )
    mocker.patch.object(entrada, "cargar_config", return_value=configuracion)
    proveedor = mocker.patch.object(entrada, "ProveedorCredencialesGoogle")
    excel = mocker.patch.object(entrada, "ejecutar_excel")
    sheets = mocker.patch.object(entrada, "ejecutar_sheets")
    assert entrada.main(["--sin-env", "--autorizar-google"]) == 0
    proveedor.assert_called_once_with(configuracion)
    proveedor.return_value.autorizar.assert_called_once_with()
    proveedor.return_value.cargar.assert_not_called()
    excel.assert_not_called()
    sheets.assert_not_called()


def test_autorizacion_en_excel_se_rechaza_antes_de_abrir_credenciales(
    configuracion: Config,
    mocker: MockerFixture,
) -> None:
    proveedor = mocker.patch.object(entrada, "ProveedorCredencialesGoogle")
    assert entrada.main(["--sin-env", "--autorizar-google"]) == 2
    proveedor.assert_not_called()


def test_sheets_se_despacha_con_las_opciones_del_comando(
    configuracion: Config,
    mocker: MockerFixture,
) -> None:
    configuracion = configuracion.model_copy(
        update={"repositorio": "sheets", "spreadsheet_id": "documento_sintetico"}
    )
    mocker.patch.object(entrada, "cargar_config", return_value=configuracion)
    excel = mocker.patch.object(entrada, "ejecutar_excel")
    sheets = mocker.patch.object(
        entrada, "ejecutar_sheets", return_value=mocker.Mock(simulacion=False, registro=None)
    )
    assert (
        entrada.main(
            [
                "--sin-env",
                "--encabezado",
                " Semana ",
                "--aplicar",
                "--permitir-pendientes",
                "--reconstruir-catalogo",
            ]
        )
        == 3
    )
    sheets.assert_called_once_with(
        configuracion,
        encabezado="Semana",
        simulacion=False,
        permitir_pendientes=True,
        forzar_catalogo=True,
    )
    excel.assert_not_called()


@pytest.mark.parametrize("caso", ["excel", "sin_id", "sin_encabezado", "rutas", "solo_lectura"])
def test_precondiciones_sheets_impiden_abrir_credenciales(
    configuracion: Config,
    mocker: MockerFixture,
    caso: str,
) -> None:
    configuracion = configuracion.model_copy(
        update={
            "repositorio": "excel" if caso == "excel" else "sheets",
            "spreadsheet_id": None if caso == "sin_id" else "documento_sintetico",
        }
    )
    if caso == "rutas":
        configuracion = configuracion.model_copy(
            update={"token_path": configuracion.contactos_path}
        )
    proveedor = mocker.patch.object(entrada, "ProveedorCredencialesGoogle")
    with pytest.raises(ConfiguracionError):
        entrada.ejecutar_sheets(
            configuracion,
            encabezado="" if caso == "sin_encabezado" else "Semana",
            simulacion=caso != "solo_lectura",
        )
    proveedor.assert_not_called()


def test_puerto_simulado_rechaza_respaldos_y_marcas_sin_io(tmp_path: Path) -> None:
    escritor = entrada._EscritorSimulado()
    with pytest.raises(ConfiguracionError):
        escritor.crear_respaldo()
    with pytest.raises(ConfiguracionError):
        escritor.aplicar_marcas("IOL", 3, "Semana", frozenset(), revision_esperada="revision")
    assert list(tmp_path.iterdir()) == []


def test_fallo_de_autorizacion_informa_operacion_explicita_sin_secretos(
    configuracion: Config,
    mocker: MockerFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mocker.patch.object(entrada, "ejecutar_excel", side_effect=AutenticacionError("secreto"))
    assert entrada.main(["--sin-env", "--encabezado", "Semana"]) == 4
    salida = capsys.readouterr()
    assert "--autorizar-google" in salida.err
    assert "secreto" not in salida.err
