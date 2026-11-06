"""Validación de documentos y conservación del destino ante fallos de disco."""

import logging
from pathlib import Path
from typing import cast
from unittest.mock import call

import pytest
from pytest_mock import MockerFixture

from automatizacion_clientes.core.errores import (
    EstructuraError,
    PersistenciaError,
    ValidacionError,
)
from automatizacion_clientes.infra.almacenamiento_json import (
    ValorJson,
    guardar_json,
    leer_json,
)


@pytest.mark.parametrize(
    "documento",
    [
        None,
        True,
        False,
        0,
        -7,
        12.5,
        "Rádono — 客户",
        [],
        {},
        {"clientes": [None, True, 42, 1.5, "Mízafe", {"activo": False}]},
    ],
)
def test_guardado_y_lectura_conservan_valores_json(tmp_path: Path, documento: ValorJson) -> None:
    ruta = tmp_path / "nueva" / "carpeta" / "documento.json"

    guardar_json(ruta, documento)

    assert leer_json(ruta) == documento
    assert list(ruta.parent.iterdir()) == [ruta]


def test_sustituye_archivo_y_genera_utf8_reproducible(tmp_path: Path) -> None:
    ruta = tmp_path / "documento.json"
    ruta.write_bytes(b"contenido anterior")

    guardar_json(ruta, {"zeta": "Mízafe", "alfa": 1})

    assert ruta.read_bytes() == '{\n  "alfa": 1,\n  "zeta": "Mízafe"\n}\n'.encode()


def test_acepta_bom_de_windows(tmp_path: Path) -> None:
    ruta = tmp_path / "documento.json"
    ruta.write_text('{"nombre": "Rádono"}', encoding="utf-8-sig")

    assert leer_json(ruta) == {"nombre": "Rádono"}


@pytest.mark.parametrize(
    "contenido",
    [
        b"",
        b"{incompleto",
        b"{} {}",
        b'{"clave": 1, "clave": 2}',
        b'{"anidado": [{"clave": 1, "clave": 2}]}',
        b"NaN",
        b"Infinity",
        b"-Infinity",
        b'{"numero": 1e9999}',
        b"[" * 2000 + b"0" + b"]" * 2000,
        b'"\xff"',
    ],
)
def test_rechaza_documentos_invalidos_sin_modificarlos(tmp_path: Path, contenido: bytes) -> None:
    ruta = tmp_path / "documento.json"
    ruta.write_bytes(contenido)

    with pytest.raises(EstructuraError) as capturada:
        leer_json(ruta)

    assert capturada.value.__cause__ is not None
    assert ruta.read_bytes() == contenido


def test_archivo_ausente_es_error_y_no_equivale_a_null(tmp_path: Path) -> None:
    ruta = tmp_path / "ausente.json"

    with pytest.raises(PersistenciaError) as capturada:
        leer_json(ruta)

    assert isinstance(capturada.value.__cause__, FileNotFoundError)
    assert not ruta.exists()


def test_traduce_error_de_permisos_de_lectura(tmp_path: Path, mocker: MockerFixture) -> None:
    causa = PermissionError("detalle técnico privado")
    mocker.patch.object(Path, "read_text", side_effect=causa)

    with pytest.raises(PersistenciaError) as capturada:
        leer_json(tmp_path / "documento.json")

    assert capturada.value.__cause__ is causa
    assert "privado" not in str(capturada.value)


@pytest.mark.parametrize(
    "documento",
    [float("nan"), float("inf"), -float("inf"), {1: "clave numérica"}, (1, 2), {1}, b"bytes"],
)
def test_rechaza_tipos_no_json_antes_de_crear_directorios(
    tmp_path: Path, documento: object
) -> None:
    ruta = tmp_path / "nueva" / "documento.json"

    with pytest.raises(ValidacionError):
        guardar_json(ruta, cast(ValorJson, documento))

    assert not ruta.parent.exists()


def test_ciclo_no_modifica_destino(tmp_path: Path) -> None:
    ruta = tmp_path / "documento.json"
    ruta.write_bytes(b"anterior")
    documento: list[ValorJson] = []
    documento.append(documento)

    with pytest.raises(ValidacionError) as capturada:
        guardar_json(ruta, documento)

    assert isinstance(capturada.value.__cause__, RecursionError)
    assert ruta.read_bytes() == b"anterior"
    assert list(tmp_path.iterdir()) == [ruta]


def test_unicode_no_codificable_no_modifica_destino(tmp_path: Path) -> None:
    ruta = tmp_path / "documento.json"
    ruta.write_bytes(b"anterior")

    with pytest.raises(ValidacionError) as capturada:
        guardar_json(ruta, {"nombre": "\ud800"})

    assert isinstance(capturada.value.__cause__, UnicodeEncodeError)
    assert ruta.read_bytes() == b"anterior"


@pytest.mark.parametrize(
    "operacion",
    [
        "pathlib.Path.mkdir",
        "automatizacion_clientes.infra.almacenamiento_json.NamedTemporaryFile",
        "automatizacion_clientes.infra.almacenamiento_json.fsync",
        "pathlib.Path.replace",
    ],
)
@pytest.mark.parametrize("destino_previo", [True, False])
def test_fallo_de_guardado_conserva_destino_y_limpia_temporales(
    tmp_path: Path, mocker: MockerFixture, operacion: str, destino_previo: bool
) -> None:
    ruta = tmp_path / "documento.json"
    if destino_previo:
        ruta.write_bytes(b"anterior")
    causa = OSError("disco no disponible")
    mocker.patch(operacion, side_effect=causa)

    with pytest.raises(PersistenciaError) as capturada:
        guardar_json(ruta, {"nuevo": True})

    assert capturada.value.__cause__ is causa
    if destino_previo:
        assert ruta.read_bytes() == b"anterior"
        assert list(tmp_path.iterdir()) == [ruta]
    else:
        assert list(tmp_path.iterdir()) == []


def test_fallo_de_limpieza_preserva_causa_principal_y_avisa_sin_datos_privados(
    tmp_path: Path, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
) -> None:
    ruta = tmp_path / "cliente-privado.json"
    ruta.write_bytes(b"anterior")
    causa = PermissionError("destino bloqueado")
    mocker.patch.object(Path, "replace", side_effect=causa)
    mocker.patch.object(Path, "unlink", side_effect=PermissionError("ruta privada"))

    with caplog.at_level(logging.WARNING), pytest.raises(PersistenciaError) as capturada:
        guardar_json(ruta, {"nombre": "Nombre privado"})

    assert capturada.value.__cause__ is causa
    assert ruta.read_bytes() == b"anterior"
    assert len(caplog.records) == 1
    assert "temporal JSON" in caplog.text
    assert "privad" not in caplog.text
    assert caplog.records[0].exc_info is None


def test_registros_de_exito_no_incluyen_rutas_ni_clientes(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ruta = tmp_path / "cliente-privado.json"

    with caplog.at_level(logging.DEBUG):
        guardar_json(ruta, {"nombre": "Nombre privado"})
        leer_json(ruta)

    assert len(caplog.records) == 2
    assert "privad" not in caplog.text


class WindowsSimuladoError(OSError):
    def __init__(self, codigo: int) -> None:
        super().__init__("Detalle privado del sistema operativo")
        self.winerror = codigo


@pytest.mark.parametrize("codigo", [32, 33])
def test_bloqueo_temporal_reintenta_solo_reemplazo_y_guarda_sin_exponer_datos(
    tmp_path: Path, mocker: MockerFixture, caplog: pytest.LogCaptureFixture, codigo: int
) -> None:
    ruta = tmp_path / "cliente-privado.json"
    ruta.write_bytes(b"anterior")
    reemplazar_original = Path.replace
    intentos = 0

    def reemplazar_con_bloqueo(origen: Path, destino: Path) -> Path:
        nonlocal intentos
        intentos += 1
        if intentos <= 2:
            assert ruta.read_bytes() == b"anterior"
            raise WindowsSimuladoError(codigo)
        return reemplazar_original(origen, destino)

    mocker.patch.object(Path, "replace", autospec=True, side_effect=reemplazar_con_bloqueo)
    esperas = mocker.patch("tenacity.nap.time.sleep")

    with caplog.at_level(logging.WARNING):
        guardar_json(ruta, {"nombre": "Nombre privado"})

    assert intentos == 3
    assert esperas.call_args_list == [call(0.05), call(0.1)]
    assert leer_json(ruta) == {"nombre": "Nombre privado"}
    assert list(tmp_path.iterdir()) == [ruta]
    assert len(caplog.records) == 2
    assert "privad" not in caplog.text
    assert all(registro.exc_info is None for registro in caplog.records)


@pytest.mark.parametrize("codigo", [32, 33])
def test_bloqueo_persistente_agota_intentos_y_conserva_destino(
    tmp_path: Path, mocker: MockerFixture, caplog: pytest.LogCaptureFixture, codigo: int
) -> None:
    ruta = tmp_path / "documento.json"
    ruta.write_bytes(b"anterior")
    causa = WindowsSimuladoError(codigo)
    reemplazo = mocker.patch.object(Path, "replace", side_effect=causa)
    esperas = mocker.patch("tenacity.nap.time.sleep")

    with caplog.at_level(logging.WARNING), pytest.raises(PersistenciaError) as capturada:
        guardar_json(ruta, {"nuevo": True})

    assert capturada.value.__cause__ is causa
    assert reemplazo.call_count == 4
    assert esperas.call_args_list == [call(0.05), call(0.1), call(0.2)]
    assert ruta.read_bytes() == b"anterior"
    assert list(tmp_path.iterdir()) == [ruta]
    assert len(caplog.records) == 3


@pytest.mark.parametrize("codigo", [5, 2, 112])
def test_error_permanente_de_reemplazo_no_se_reintenta(
    tmp_path: Path, mocker: MockerFixture, codigo: int
) -> None:
    ruta = tmp_path / "documento.json"
    ruta.write_bytes(b"anterior")
    reemplazo = mocker.patch.object(Path, "replace", side_effect=WindowsSimuladoError(codigo))
    esperas = mocker.patch("tenacity.nap.time.sleep")

    with pytest.raises(PersistenciaError):
        guardar_json(ruta, {"nuevo": True})

    reemplazo.assert_called_once()
    esperas.assert_not_called()
    assert ruta.read_bytes() == b"anterior"
