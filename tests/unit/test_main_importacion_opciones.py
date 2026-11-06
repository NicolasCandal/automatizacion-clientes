"""Opciones del comando de importación sin archivos de chats reales."""

from datetime import date
from os import environ
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from automatizacion_clientes import main as entrada
from automatizacion_clientes.config import Config
from automatizacion_clientes.core.errores import ConfiguracionError
from automatizacion_clientes.core.modelos import PeriodoSemanal

PERIODO = PeriodoSemanal(desde=date(2026, 11, 2), hasta=date(2026, 11, 6))


@pytest.fixture(autouse=True)
def aislar_entorno(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for nombre in tuple(environ):
        if nombre.upper().startswith("AC_"):
            monkeypatch.delenv(nombre)
    monkeypatch.chdir(tmp_path)


def argumentos(*opciones: str) -> list[str]:
    return [
        "--sin-env",
        "--importar-whatsapp",
        "--desde",
        "2026-11-02",
        "--hasta",
        "2026-11-06",
        *opciones,
    ]


@pytest.mark.parametrize(
    "opciones",
    [
        ["--importar-whatsapp"],
        ["--importar-whatsapp", "--desde", "2026-11-02"],
        ["--importar-whatsapp", "--hasta", "2026-11-06"],
        ["--importar-whatsapp", "--desde", "2026-11-07", "--hasta", "2026-11-02"],
        ["--importar-whatsapp", "--desde", "2026-11-31", "--hasta", "2026-12-01"],
        ["--importar-whatsapp", "--desde", "20261102", "--hasta", "2026-11-06"],
        ["--importar-whatsapp", "--desde", "2026-W45-1", "--hasta", "2026-11-06"],
        ["--importar-whatsapp", "--desde", "secreto", "--hasta", "2026-11-06"],
        ["--importar-whatsapp", "--autorizar-google"],
        ["--encabezado", "Semana", "--desde", "2026-11-02"],
        ["--autorizar-google", "--hasta", "2026-11-06"],
        ["--encabezado", "Semana", "--manifiesto", "secreto.json"],
        ["--autorizar-google", "--manifiesto", "secreto.json"],
        ["--manifiesto", "secreto.json"],
    ],
)
def test_opciones_incompatibles_se_rechazan_antes_de_cargar_configuracion(
    opciones: list[str],
    mocker: MockerFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    carga = mocker.patch.object(entrada, "cargar_config")
    importar = mocker.patch.object(entrada, "ejecutar_importacion")
    assert entrada.main(opciones) == 2
    carga.assert_not_called()
    importar.assert_not_called()
    salida = capsys.readouterr()
    assert salida.out == ""
    assert "secreto" not in salida.err
    assert "Traceback" not in salida.err


@pytest.mark.parametrize(
    "opciones",
    [
        ["--encabezado", "Semana"],
        ["--permitir-pendientes"],
        ["--reconstruir-catalogo"],
    ],
)
def test_importacion_no_acepta_opciones_de_registro(
    opciones: list[str],
    mocker: MockerFixture,
) -> None:
    carga = mocker.patch.object(entrada, "cargar_config")
    assert entrada.main(argumentos(*opciones)) == 2
    carga.assert_not_called()


@pytest.mark.parametrize(
    ("opcion", "esperada"), [((), None), (("--simular",), True), (("--aplicar",), False)]
)
def test_comando_transmite_periodo_y_modo_sin_ejecutar_registro(
    tmp_path: Path,
    mocker: MockerFixture,
    opcion: tuple[str, ...],
    esperada: bool | None,
) -> None:
    configuracion = Config(directorio_base=tmp_path, _env_file=None)
    mocker.patch.object(entrada, "cargar_config", return_value=configuracion)
    resultado = mocker.Mock(
        simulacion=True,
        extraccion=mocker.Mock(total_chats_escaneados=2, semana=mocker.Mock(contactos=())),
    )
    importar = mocker.patch.object(entrada, "ejecutar_importacion", return_value=resultado)
    excel = mocker.patch.object(entrada, "ejecutar_excel")
    sheets = mocker.patch.object(entrada, "ejecutar_sheets")
    autorizar = mocker.patch.object(entrada, "autorizar_google")
    assert entrada.main(argumentos(*opcion)) == 0
    importar.assert_called_once_with(configuracion, periodo=PERIODO, simulacion=esperada)
    excel.assert_not_called()
    sheets.assert_not_called()
    autorizar.assert_not_called()


@pytest.mark.parametrize("absoluta", [False, True])
def test_manifiesto_explicito_se_resuelve_desde_base_configurada(
    tmp_path: Path,
    mocker: MockerFixture,
    absoluta: bool,
) -> None:
    base = tmp_path / "base_configurada"
    configuracion = Config(directorio_base=base, _env_file=None)
    mocker.patch.object(entrada, "cargar_config", return_value=configuracion)
    importar = mocker.patch.object(entrada, "ejecutar_importacion")
    importar.return_value.extraccion.semana.contactos = ()
    ruta = base / "lote.json" if absoluta else Path("lote.json")
    assert entrada.main(argumentos("--manifiesto", str(ruta))) == 0
    recibida = importar.call_args.args[0]
    assert recibida.whatsapp_manifiesto_path == base / "lote.json"
    assert configuracion.whatsapp_manifiesto_path != recibida.whatsapp_manifiesto_path
    assert not base.exists()


@pytest.mark.parametrize(
    "campo", ["token_path", "credentials_path", "contactos_path", "excel_path"]
)
def test_destino_no_puede_ocupar_archivos_de_entrada_o_secretos(
    tmp_path: Path,
    mocker: MockerFixture,
    campo: str,
) -> None:
    configuracion = Config(directorio_base=tmp_path, _env_file=None)
    configuracion = configuracion.model_copy(
        update={"contactos_path": getattr(configuracion, campo)}
    )
    if campo == "contactos_path":
        configuracion = configuracion.model_copy(
            update={"contactos_path": configuracion.whatsapp_manifiesto_path}
        )
    extractor = mocker.patch.object(entrada, "ExtractorWhatsAppTxt")
    with pytest.raises(ConfiguracionError):
        entrada.ejecutar_importacion(configuracion, periodo=PERIODO, simulacion=False)
    extractor.assert_not_called()


def test_ayuda_incluye_importacion_sin_leer_configuracion(
    mocker: MockerFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    carga = mocker.patch.object(entrada, "cargar_config")
    assert entrada.main(["--help"]) == 0
    carga.assert_not_called()
    ayuda = capsys.readouterr().err
    for opcion in ("--importar-whatsapp", "--desde", "--hasta", "--manifiesto"):
        assert opcion in ayuda
