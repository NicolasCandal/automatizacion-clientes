"""Comando de importación completo sobre TXT y JSON ficticios temporales."""

import json
import subprocess
import sys
from collections.abc import Iterable, Iterator
from datetime import date
from os import environ
from pathlib import Path
from typing import cast

import pytest
from pytest_mock import MockerFixture

from automatizacion_clientes import main as entrada
from automatizacion_clientes.config import Config
from automatizacion_clientes.core.importacion_whatsapp import (
    ActividadChat,
    OrdenFecha,
    analizar_actividad,
)
from automatizacion_clientes.core.modelos import PeriodoSemanal
from automatizacion_clientes.infra import almacenamiento_json, extractor_whatsapp_txt
from automatizacion_clientes.infra.lector_contactos_json import LectorContactosJson


@pytest.fixture(autouse=True)
def aislar_entorno(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture
) -> Iterator[None]:
    for nombre in tuple(environ):
        if nombre.upper().startswith("AC_"):
            monkeypatch.delenv(nombre)
    monkeypatch.chdir(tmp_path)
    operaciones = [
        mocker.patch.object(
            entrada, nombre, side_effect=AssertionError("Operación ajena a la importación")
        )
        for nombre in (
            "ProveedorCredencialesGoogle",
            "LectorClientesExcel",
            "crear_lector_sheets",
            "crear_escritor_sheets",
            "ejecutar_excel",
            "ejecutar_sheets",
        )
    ]
    mocker.patch("requests.sessions.Session.send", side_effect=AssertionError("No se permite red"))
    mocker.patch("webbrowser.open", side_effect=AssertionError("No se permite navegador"))
    yield
    for operacion in operaciones:
        operacion.assert_not_called()


@pytest.fixture
def configuracion(tmp_path: Path) -> Config:
    configuracion = Config(directorio_base=tmp_path, _env_file=None)
    carpeta = configuracion.whatsapp_manifiesto_path.parent
    carpeta.mkdir(parents=True)
    (carpeta / "axa.txt").write_text(
        "02/11/2026, 10:00 - Axa: contenido ficticio privado\n"
        "05/11/2026, 10:00 - Yo: otra línea ficticia privada\n"
        "10/11/2026, 10:00 - Axa: mensaje posterior\n",
        encoding="utf-8",
    )
    (carpeta / "grupo.txt").write_text(
        "[03/11/2026, 10:00:30] Yo: mensaje ficticio de grupo\n", encoding="utf-8"
    )
    documento = {
        "version_esquema": 1,
        "orden_fecha": "dia_mes",
        "chats": [
            {
                "archivo": "axa.txt",
                "nombre_crudo": "Axa Prueba (IOL)",
                "telefono": "+5491100000000",
                "es_grupo": False,
                "remitentes": ["Yo", "Axa"],
            },
            {
                "archivo": "grupo.txt",
                "nombre_crudo": "Grupo de ejemplo",
                "es_grupo": True,
                "remitentes": ["Yo"],
            },
        ],
    }
    configuracion.whatsapp_manifiesto_path.write_text(json.dumps(documento), encoding="utf-8")
    return configuracion


def argumentos(configuracion: Config, *opciones: str) -> list[str]:
    return [
        "--sin-env",
        "--directorio-base",
        str(configuracion.directorio_base),
        "--importar-whatsapp",
        "--desde",
        "2026-11-02",
        "--hasta",
        "2026-11-06",
        *opciones,
    ]


def archivos(base: Path) -> dict[Path, bytes]:
    return {ruta.relative_to(base): ruta.read_bytes() for ruta in base.rglob("*") if ruta.is_file()}


def test_simulacion_predeterminada_no_guarda_ni_filtra_datos_en_logs(
    configuracion: Config,
    capsys: pytest.CaptureFixture[str],
) -> None:
    originales = archivos(configuracion.directorio_base)
    assert entrada.main(argumentos(configuracion)) == 0
    assert archivos(configuracion.directorio_base) == originales
    salida = capsys.readouterr()
    assert salida.out == ""
    assert "simulación=True; 2 chats; 2 contactos" in salida.err
    for secreto in (
        "Axa Prueba",
        "+54911",
        "contenido ficticio",
        str(configuracion.whatsapp_manifiesto_path),
    ):
        assert secreto not in salida.err


def test_aplicar_genera_solo_entrada_semanal_y_conserva_fuentes(configuracion: Config) -> None:
    configuracion.contactos_path.write_bytes(b"salida anterior")
    originales = archivos(configuracion.directorio_base)
    assert entrada.main(argumentos(configuracion, "--aplicar")) == 0
    semana = LectorContactosJson(configuracion.contactos_path).leer()
    assert semana.periodo == PeriodoSemanal(desde=date(2026, 11, 2), hasta=date(2026, 11, 6))
    assert semana.contactos[0].ultima_fecha == date(2026, 11, 5)
    assert semana.contactos[1].es_grupo
    assert "contenido ficticio" not in configuracion.contactos_path.read_text(encoding="utf-8")
    actuales = archivos(configuracion.directorio_base)
    clave = configuracion.contactos_path.relative_to(configuracion.directorio_base)
    actuales.pop(clave)
    originales.pop(clave)
    assert actuales == originales
    assert not configuracion.directorio_respaldos.exists()
    assert not configuracion.resolucion_path.exists()


@pytest.mark.parametrize("simular", [False, True])
def test_modo_del_entorno_y_override_se_respetan(
    configuracion: Config,
    monkeypatch: pytest.MonkeyPatch,
    simular: bool,
) -> None:
    monkeypatch.setenv("AC_SIMULACION", "false")
    assert entrada.main(argumentos(configuracion, *(["--simular"] if simular else []))) == 0
    assert configuracion.contactos_path.exists() is not simular


def test_repositorio_sheets_no_requiere_credenciales_para_importar(
    configuracion: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AC_REPOSITORIO", "sheets")
    monkeypatch.setenv("AC_SPREADSHEET_ID", "documento_ficticio")
    assert entrada.main(argumentos(configuracion, "--aplicar")) == 0
    assert configuracion.contactos_path.exists()
    assert not configuracion.token_path.exists()
    assert not configuracion.credentials_path.exists()


def test_manifiesto_por_argumento_prevalece_sobre_entorno(
    configuracion: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ruta = configuracion.whatsapp_manifiesto_path
    monkeypatch.setenv("AC_WHATSAPP_MANIFIESTO_PATH", "ausente.json")
    relativa = ruta.relative_to(configuracion.directorio_base)
    assert entrada.main(argumentos(configuracion, "--manifiesto", str(relativa), "--aplicar")) == 0
    assert configuracion.contactos_path.exists()


def test_archivo_de_entorno_configura_manifiesto_y_destino(configuracion: Config) -> None:
    ruta_env = configuracion.directorio_base / "opciones.env"
    ruta_env.write_text(
        "AC_CONTACTOS_PATH=salida/semana.json\nAC_SIMULACION=false\n", encoding="utf-8"
    )
    assert (
        entrada.main(
            [
                "--env-file",
                "opciones.env",
                "--directorio-base",
                str(configuracion.directorio_base),
                "--importar-whatsapp",
                "--desde",
                "2026-11-02",
                "--hasta",
                "2026-11-06",
            ]
        )
        == 0
    )
    assert (configuracion.directorio_base / "salida/semana.json").exists()
    assert not configuracion.contactos_path.exists()


def test_un_error_del_lote_conserva_salida_anterior(configuracion: Config) -> None:
    configuracion.contactos_path.write_bytes(b"salida anterior")
    (configuracion.whatsapp_manifiesto_path.parent / "grupo.txt").write_bytes(b"\xff")
    originales = archivos(configuracion.directorio_base)
    assert entrada.main(argumentos(configuracion, "--aplicar")) == 2
    assert archivos(configuracion.directorio_base) == originales


def test_manifiesto_ausente_devuelve_cuatro_sin_crear_salida(configuracion: Config) -> None:
    assert entrada.main(argumentos(configuracion, "--manifiesto", "ausente.json", "--aplicar")) == 4
    assert not configuracion.contactos_path.exists()


def test_cambio_de_fuente_devuelve_cinco_sin_guardar(
    configuracion: Config,
    mocker: MockerFixture,
) -> None:
    def analizar_y_cambiar(
        lineas: Iterable[str],
        *,
        periodo: PeriodoSemanal,
        remitentes: tuple[str, ...],
        orden_fecha: OrdenFecha,
        siglo_anio_corto: int | None = None,
    ) -> ActividadChat:
        resultado = analizar_actividad(
            lineas,
            periodo=periodo,
            remitentes=remitentes,
            orden_fecha=orden_fecha,
            siglo_anio_corto=siglo_anio_corto,
        )
        ruta = configuracion.whatsapp_manifiesto_path.parent / "axa.txt"
        ruta.write_bytes(ruta.read_bytes() + b"\n")
        return resultado

    mocker.patch.object(
        extractor_whatsapp_txt, "analizar_actividad", side_effect=analizar_y_cambiar
    )
    assert entrada.main(argumentos(configuracion, "--aplicar")) == 5
    assert not configuracion.contactos_path.exists()


def test_fallo_de_reemplazo_conserva_salida_y_limpia_temporal(
    configuracion: Config,
    mocker: MockerFixture,
) -> None:
    configuracion.contactos_path.write_bytes(b"salida anterior")
    originales = archivos(configuracion.directorio_base)
    mocker.patch.object(almacenamiento_json, "_reemplazar_temporal", side_effect=OSError("secreto"))
    assert entrada.main(argumentos(configuracion, "--aplicar")) == 4
    assert archivos(configuracion.directorio_base) == originales


@pytest.mark.parametrize("campo", ["whatsapp_manifiesto_path", "token_path", "credentials_path"])
def test_rutas_coincidentes_se_rechazan_sin_alterar_entradas(
    configuracion: Config,
    monkeypatch: pytest.MonkeyPatch,
    campo: str,
) -> None:
    ruta = cast(Path, getattr(configuracion, campo))
    monkeypatch.setenv("AC_CONTACTOS_PATH", str(ruta))
    originales = archivos(configuracion.directorio_base)
    assert entrada.main(argumentos(configuracion, "--aplicar")) == 2
    assert archivos(configuracion.directorio_base) == originales


def test_no_se_puede_usar_archivo_env_como_manifiesto(configuracion: Config) -> None:
    ruta_env = configuracion.directorio_base / "opciones.env"
    ruta_env.write_text("AC_SIMULACION=true\n", encoding="utf-8")
    original = ruta_env.read_bytes()
    assert (
        entrada.main(
            [
                "--env-file",
                "opciones.env",
                "--directorio-base",
                str(configuracion.directorio_base),
                "--importar-whatsapp",
                "--desde",
                "2026-11-02",
                "--hasta",
                "2026-11-06",
                "--manifiesto",
                "opciones.env",
            ]
        )
        == 2
    )
    assert ruta_env.read_bytes() == original


def test_sin_actividad_genera_una_semana_vacia_valida(configuracion: Config) -> None:
    opciones = argumentos(configuracion, "--aplicar")
    opciones[opciones.index("2026-11-02")] = "2027-01-01"
    opciones[opciones.index("2026-11-06")] = "2027-01-07"
    assert entrada.main(opciones) == 0
    assert LectorContactosJson(configuracion.contactos_path).leer().contactos == ()


def test_modulo_ejecutable_importa_en_subproceso_sin_archivos_reales(configuracion: Config) -> None:
    proceso = subprocess.run(
        [sys.executable, "-m", "automatizacion_clientes", *argumentos(configuracion, "--aplicar")],
        cwd=configuracion.directorio_base,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert proceso.returncode == 0
    assert proceso.stdout == ""
    assert len(LectorContactosJson(configuracion.contactos_path).leer().contactos) == 2
    assert not configuracion.directorio_respaldos.exists()
