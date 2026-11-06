"""Pruebas aisladas de configuración sin credenciales ni acceso a servicios."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from automatizacion_clientes.config import Config, cargar_config
from automatizacion_clientes.core.modelos import PoliticaCoincidencia


@pytest.fixture(autouse=True)
def aislar_entorno(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Evita que el entorno y el archivo .env locales afecten los tests."""
    from os import environ

    for nombre in tuple(environ):
        if nombre.upper().startswith("AC_"):
            monkeypatch.delenv(nombre)
    monkeypatch.chdir(tmp_path)


def test_valores_por_defecto_son_seguros(tmp_path: Path) -> None:
    configuracion = cargar_config(ruta_env=None)

    assert configuracion.repositorio == "excel"
    assert configuracion.simulacion is True
    assert configuracion.spreadsheet_id is None
    assert configuracion.excel_path == tmp_path / "datos/entrada/clientes.xlsx"
    assert configuracion.resolucion_path == tmp_path / "datos/salida/resolucion.json"
    assert configuracion.credentials_path == tmp_path / "secretos/credentials.json"
    assert configuracion.zona_horaria == "America/Argentina/Buenos_Aires"
    assert not (tmp_path / "datos").exists()
    assert not (tmp_path / "secretos").exists()
    assert not (tmp_path / "logs").exists()


def test_entorno_prevalece_sobre_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    archivo_env = tmp_path / ".env"
    archivo_env.write_text("AC_LOG_LEVEL=WARNING\nAC_SIMULACION=false\n", encoding="utf-8")
    monkeypatch.setenv("AC_LOG_LEVEL", "ERROR")

    configuracion = cargar_config()

    assert configuracion.log_level == "ERROR"
    assert configuracion.simulacion is False


def test_argumento_explicito_prevalece_sobre_entorno(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AC_LOG_LEVEL", "ERROR")

    assert Config(log_level="DEBUG", _env_file=None).log_level == "DEBUG"


def test_se_puede_desactivar_dotenv(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("AC_REPOSITORIO=invalido\n", encoding="utf-8")

    assert cargar_config(ruta_env=None).repositorio == "excel"


def test_dotenv_puede_compartirse_con_la_aplicacion_anterior(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "SPREADSHEET_ID=identificador_anterior\nAC_LOG_LEVEL=WARNING\n", encoding="utf-8"
    )

    configuracion = cargar_config()

    assert configuracion.spreadsheet_id is None
    assert configuracion.log_level == "WARNING"


def test_base_explicita_resuelve_archivo_env_y_rutas(tmp_path: Path) -> None:
    base_configurada = tmp_path / "otra_base"
    base_configurada.mkdir()
    (base_configurada / "opciones.env").write_text(
        "AC_EXCEL_PATH=entrada/clientes.xlsx\n", encoding="utf-8"
    )

    configuracion = cargar_config(Path("opciones.env"), directorio_base=base_configurada)

    assert configuracion.excel_path == base_configurada / "entrada/clientes.xlsx"
    assert configuracion.directorio_base == base_configurada


def test_base_del_entorno_resuelve_rutas(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    base_configurada = tmp_path / "base_desde_entorno"
    monkeypatch.setenv("AC_DIRECTORIO_BASE", str(base_configurada))

    assert cargar_config(ruta_env=None).excel_path == (
        base_configurada / "datos/entrada/clientes.xlsx"
    )


def test_base_explicita_prevalece_sobre_entorno(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AC_DIRECTORIO_BASE", str(tmp_path / "base_del_entorno"))

    configuracion = cargar_config(ruta_env=None, directorio_base=tmp_path / "base_explicita")

    assert configuracion.directorio_base == tmp_path / "base_explicita"


def test_ruta_absoluta_no_se_reinterpreta(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ruta_absoluta = tmp_path / "ubicacion_independiente/clientes.xlsx"
    monkeypatch.setenv("AC_EXCEL_PATH", str(ruta_absoluta))

    configuracion = cargar_config(ruta_env=None, directorio_base=tmp_path / "otra_base")

    assert configuracion.excel_path == ruta_absoluta


@pytest.mark.parametrize("identificador", ["", "   "])
def test_sheets_requiere_identificador_no_vacio(
    monkeypatch: pytest.MonkeyPatch, identificador: str
) -> None:
    monkeypatch.setenv("AC_REPOSITORIO", "sheets")
    monkeypatch.setenv("AC_SPREADSHEET_ID", identificador)

    with pytest.raises(ValidationError, match="AC_SPREADSHEET_ID"):
        cargar_config(ruta_env=None)


def test_sheets_requiere_identificador_ausente(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AC_REPOSITORIO", "sheets")

    with pytest.raises(ValidationError, match="AC_SPREADSHEET_ID"):
        cargar_config(ruta_env=None)


def test_sheets_no_abre_credenciales_al_cargar(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AC_REPOSITORIO", "sheets")
    monkeypatch.setenv("AC_SPREADSHEET_ID", "  identificador_de_prueba  ")

    configuracion = cargar_config(ruta_env=None)

    assert configuracion.spreadsheet_id == "identificador_de_prueba"
    assert not configuracion.credentials_path.exists()
    assert "identificador_de_prueba" not in repr(configuracion)


@pytest.mark.parametrize(
    ("nombre", "valor"),
    [
        ("AC_REINTENTOS_MAXIMOS", "0"),
        ("AC_REINTENTOS_MAXIMOS", "11"),
        ("AC_TIMEOUT_RED_SEGUNDOS", "0"),
        ("AC_TIMEOUT_RED_SEGUNDOS", "nan"),
        ("AC_TIMEOUT_RED_SEGUNDOS", "inf"),
        ("AC_REINTENTO_ESPERA_MINIMA_SEGUNDOS", "-1"),
        ("AC_REINTENTO_ESPERA_MAXIMA_SEGUNDOS", "inf"),
        ("AC_REPOSITORIO", "inexistente"),
        ("AC_LOG_LEVEL", "INVALIDO"),
        ("AC_ENTORNO", "invalido"),
        ("AC_SIMULACION", "quizas"),
        ("AC_ZONA_HORARIA", "Zona/Inexistente"),
        ("AC_EXCEL_PATH", "   "),
        ("AC_RESOLUCION_PATH", "   "),
        ("AC_DIRECTORIO_BASE", ""),
    ],
)
def test_rechaza_opciones_invalidas(
    monkeypatch: pytest.MonkeyPatch, nombre: str, valor: str
) -> None:
    monkeypatch.setenv(nombre, valor)

    with pytest.raises(ValidationError):
        cargar_config(ruta_env=None)


def test_rechaza_esperas_invertidas(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AC_REINTENTO_ESPERA_MINIMA_SEGUNDOS", "60")
    monkeypatch.setenv("AC_REINTENTO_ESPERA_MAXIMA_SEGUNDOS", "30")

    with pytest.raises(ValidationError, match="espera mínima"):
        cargar_config(ruta_env=None)


def test_token_no_puede_sobrescribir_credenciales(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AC_CREDENTIALS_PATH", "secretos/mismo_archivo.json")
    monkeypatch.setenv("AC_TOKEN_PATH", "secretos/mismo_archivo.json")

    with pytest.raises(ValidationError, match="archivos distintos"):
        cargar_config(ruta_env=None)


def test_configuracion_es_inmutable() -> None:
    configuracion = cargar_config(ruta_env=None)

    with pytest.raises(ValidationError, match="frozen"):
        configuracion.__setattr__("simulacion", False)


def test_ruta_del_informe_se_configura_desde_entorno_y_base(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AC_RESOLUCION_PATH", "informes/resultado.json")
    base = tmp_path / "base_configurada"

    configuracion = cargar_config(ruta_env=None, directorio_base=base)

    assert configuracion.resolucion_path == base / "informes/resultado.json"
    assert not base.exists()


def test_cargas_sucesivas_no_reutilizan_configuracion(monkeypatch: pytest.MonkeyPatch) -> None:
    assert cargar_config(ruta_env=None).log_level == "INFO"
    monkeypatch.setenv("AC_LOG_LEVEL", "DEBUG")

    assert cargar_config(ruta_env=None).log_level == "DEBUG"


def test_politica_predeterminada_coincide_con_el_dominio() -> None:
    assert cargar_config(ruta_env=None).crear_politica_coincidencia() == PoliticaCoincidencia()


def test_politica_se_puede_configurar_desde_el_entorno(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AC_UMBRAL_COINCIDENCIA_AUTOMATICO", "98")
    monkeypatch.setenv("AC_UMBRAL_COINCIDENCIA_REVISION", "90")
    monkeypatch.setenv("AC_LIMITE_SUGERENCIAS_COINCIDENCIA", "5")

    assert cargar_config(ruta_env=None).crear_politica_coincidencia() == PoliticaCoincidencia(
        umbral_automatico=98.0, umbral_revision=90.0, limite_sugerencias=5
    )


@pytest.mark.parametrize(
    ("automatico", "revision", "limite"),
    [
        ("80", "80", "3"),
        ("80", "92", "3"),
        ("101", "80", "3"),
        ("nan", "80", "3"),
        ("92", "-1", "3"),
        ("92", "80", "0"),
    ],
)
def test_configuracion_rechaza_politica_inconsistente(
    monkeypatch: pytest.MonkeyPatch, automatico: str, revision: str, limite: str
) -> None:
    monkeypatch.setenv("AC_UMBRAL_COINCIDENCIA_AUTOMATICO", automatico)
    monkeypatch.setenv("AC_UMBRAL_COINCIDENCIA_REVISION", revision)
    monkeypatch.setenv("AC_LIMITE_SUGERENCIAS_COINCIDENCIA", limite)

    with pytest.raises(ValidationError):
        cargar_config(ruta_env=None)
