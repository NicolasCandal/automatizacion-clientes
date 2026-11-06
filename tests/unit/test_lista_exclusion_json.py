"""Normalización, compatibilidad histórica y persistencia inmediata de exclusiones."""

import logging
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from automatizacion_clientes.core.contratos import ListaExclusion
from automatizacion_clientes.core.errores import (
    ConcurrenciaError,
    EstructuraError,
    PersistenciaError,
    ValidacionError,
)
from automatizacion_clientes.infra.almacenamiento_json import ValorJson, guardar_json, leer_json
from automatizacion_clientes.infra.lista_exclusion_json import ListaExclusionJson


def test_archivo_ausente_inicia_lista_vacia_sin_crear_carpetas(tmp_path: Path) -> None:
    ruta = tmp_path / "nueva" / "exclusiones.json"
    lista: ListaExclusion = ListaExclusionJson(ruta)

    assert not lista.contiene("El Contador")
    assert not ruta.parent.exists()


def test_carga_formato_historico_sin_modificar_originales(tmp_path: Path) -> None:
    ruta = tmp_path / "exclusiones.json"
    guardar_json(ruta, ["  KÚKEVE, Rádono  ", "El Contador", "Proveedor (IOL)"])
    contenido_previo = ruta.read_bytes()
    lista: ListaExclusion = ListaExclusionJson(ruta)

    assert lista.contiene("kukeve radono")
    assert lista.contiene(" EL   CONTADOR ")
    assert lista.contiene("proveedor iol")
    assert not lista.contiene("Radono Kukeve")
    assert not lista.contiene("Proveedor")
    assert not lista.contiene("El Contadorcito")
    assert ruta.read_bytes() == contenido_previo


def test_consulta_usa_instantanea_sin_acceder_al_disco(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    ruta = tmp_path / "exclusiones.json"
    guardar_json(ruta, ["El Contador"])
    lista = ListaExclusionJson(ruta)
    lectura = mocker.patch.object(Path, "read_text", side_effect=PermissionError("bloqueado"))

    assert lista.contiene("El Contador")
    assert not lista.contiene("Otro contacto")
    lectura.assert_not_called()


def test_agregar_guarda_inmediatamente_y_conserva_el_nombre_original(tmp_path: Path) -> None:
    ruta = tmp_path / "nueva" / "exclusiones.json"
    lista: ListaExclusion = ListaExclusionJson(ruta)
    nombre = "  KÚKEVE, Rádono  "

    lista.agregar(nombre)

    assert lista.contiene("kukeve radono")
    assert leer_json(ruta) == [nombre]
    assert ListaExclusionJson(ruta).contiene("kukeve radono")
    assert list(ruta.parent.iterdir()) == [ruta]


def test_altas_sucesivas_preservan_entradas_y_guardan_en_orden(tmp_path: Path) -> None:
    ruta = tmp_path / "exclusiones.json"
    lista = ListaExclusionJson(ruta)

    lista.agregar("Proveedor Z")
    lista.agregar("Contador A")
    lista.agregar("Familia")

    assert leer_json(ruta) == ["Contador A", "Familia", "Proveedor Z"]
    assert lista.contiene("proveedor z")
    assert lista.contiene("contador a")
    assert lista.contiene("familia")


def test_nombre_equivalente_es_idempotente_y_no_vuelve_a_escribir(
    tmp_path: Path, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
) -> None:
    ruta = tmp_path / "exclusiones.json"
    lista = ListaExclusionJson(ruta)
    lista.agregar("  KÚKEVE, Rádono  ")
    contenido_previo = ruta.read_bytes()
    escritura = mocker.patch("automatizacion_clientes.infra.lista_exclusion_json.guardar_json")

    with caplog.at_level(logging.INFO):
        lista.agregar("kukeve radono")
        lista.agregar("KÚKEVE, RÁDONO")

    escritura.assert_not_called()
    assert ruta.read_bytes() == contenido_previo
    assert not caplog.records


def test_duplicados_historicos_se_conservan_sin_agregar_equivalentes(tmp_path: Path) -> None:
    ruta = tmp_path / "exclusiones.json"
    originales = ["Rádono Kúkeve", "radono kukeve", "Rádono Kúkeve"]
    documento: list[ValorJson] = list(originales)
    guardar_json(ruta, documento)
    lista = ListaExclusionJson(ruta)

    lista.agregar("RÁDONO KÚKEVE")
    assert leer_json(ruta) == originales
    lista.agregar("El Contador")

    assert leer_json(ruta) == sorted([*originales, "El Contador"])
    assert lista.contiene("radono kukeve")


@pytest.mark.parametrize("nombre", ["", "  \t\n", "!!!", "--- ()", "\u0301"])
def test_nombre_sin_texto_significativo_se_rechaza_antes_de_leer_o_escribir(
    tmp_path: Path, nombre: str, mocker: MockerFixture
) -> None:
    ruta = tmp_path / "nueva" / "exclusiones.json"
    lista = ListaExclusionJson(ruta)
    lectura = mocker.patch.object(Path, "read_text", side_effect=PermissionError("bloqueado"))

    with pytest.raises(ValidacionError, match="texto significativo"):
        lista.agregar(nombre)

    assert not lista.contiene(nombre)
    assert not ruta.parent.exists()
    lectura.assert_not_called()


@pytest.mark.parametrize("nombre", ["", "  ", "!!!", "\u0301"])
def test_consulta_sin_texto_significativo_devuelve_false(tmp_path: Path, nombre: str) -> None:
    assert not ListaExclusionJson(tmp_path / "exclusiones.json").contiene(nombre)


@pytest.mark.parametrize("documento", [None, {}, "Nombre", 1, [None], [False], [42], [["Nombre"]]])
def test_esquema_incorrecto_no_se_interpreta_como_lista_vacia(
    tmp_path: Path, documento: ValorJson
) -> None:
    ruta = tmp_path / "exclusiones.json"
    guardar_json(ruta, documento)
    contenido_previo = ruta.read_bytes()

    with pytest.raises(EstructuraError):
        ListaExclusionJson(ruta)

    assert ruta.read_bytes() == contenido_previo


@pytest.mark.parametrize("nombre", ["", "  ", "!!!"])
def test_entrada_persistida_sin_texto_significativo_se_rechaza(tmp_path: Path, nombre: str) -> None:
    ruta = tmp_path / "exclusiones.json"
    guardar_json(ruta, ["Nombre válido", nombre])
    contenido_previo = ruta.read_bytes()

    with pytest.raises(ValidacionError):
        ListaExclusionJson(ruta)

    assert ruta.read_bytes() == contenido_previo


def test_json_corrupto_no_se_interpreta_como_lista_vacia(tmp_path: Path) -> None:
    ruta = tmp_path / "exclusiones.json"
    ruta.write_bytes(b"[incompleto")

    with pytest.raises(EstructuraError):
        ListaExclusionJson(ruta)

    assert ruta.read_bytes() == b"[incompleto"


def test_error_de_acceso_al_cargar_no_se_confunde_con_ausencia(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    causa = PermissionError("detalle privado")
    mocker.patch.object(Path, "read_text", side_effect=causa)

    with pytest.raises(PersistenciaError) as capturada:
        ListaExclusionJson(tmp_path / "exclusiones.json")

    assert capturada.value.__cause__ is causa
    assert "privado" not in str(capturada.value)


@pytest.mark.parametrize("archivo_previo", [True, False])
@pytest.mark.parametrize(
    "operacion",
    ["automatizacion_clientes.infra.almacenamiento_json.fsync", "pathlib.Path.replace"],
)
def test_fallo_de_guardado_no_deja_exclusion_solo_en_memoria_y_permite_reintentar(
    tmp_path: Path, mocker: MockerFixture, archivo_previo: bool, operacion: str
) -> None:
    ruta = tmp_path / "exclusiones.json"
    if archivo_previo:
        guardar_json(ruta, ["Anterior"])
    lista = ListaExclusionJson(ruta)
    causa = OSError("archivo bloqueado")
    fallo = mocker.patch(operacion, side_effect=causa)

    with pytest.raises(PersistenciaError) as capturada:
        lista.agregar("Nuevo")

    assert capturada.value.__cause__ is causa
    assert not lista.contiene("Nuevo")
    if archivo_previo:
        assert lista.contiene("Anterior")
        assert leer_json(ruta) == ["Anterior"]
        assert list(tmp_path.iterdir()) == [ruta]
    else:
        assert list(tmp_path.iterdir()) == []
    mocker.stop(fallo)
    lista.agregar("Nuevo")

    assert lista.contiene("Nuevo")
    assert ListaExclusionJson(ruta).contiene("Nuevo")


def test_error_al_comprobar_archivo_no_modifica_estado(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    ruta = tmp_path / "exclusiones.json"
    lista = ListaExclusionJson(ruta)
    lectura = mocker.patch.object(Path, "read_text", side_effect=PermissionError("bloqueado"))

    with pytest.raises(PersistenciaError):
        lista.agregar("Nuevo")

    assert not lista.contiene("Nuevo")
    assert not ruta.exists()
    mocker.stop(lectura)
    lista.agregar("Nuevo")
    assert lista.contiene("Nuevo")


def test_texto_no_codificable_no_se_guarda_ni_se_incorpora_a_la_sesion(tmp_path: Path) -> None:
    ruta = tmp_path / "exclusiones.json"
    lista = ListaExclusionJson(ruta)

    with pytest.raises(ValidacionError):
        lista.agregar("Axa\ud800")

    assert not lista.contiene("Axa")
    assert not ruta.exists()


@pytest.mark.parametrize("cambio", ["creacion", "modificacion", "eliminacion"])
def test_cambio_externo_no_se_sobrescribe_ni_modifica_la_instantanea(
    tmp_path: Path, cambio: str
) -> None:
    ruta = tmp_path / "exclusiones.json"
    if cambio != "creacion":
        guardar_json(ruta, ["Anterior"])
    lista = ListaExclusionJson(ruta)
    if cambio == "eliminacion":
        ruta.unlink()
        contenido_externo = None
    else:
        guardar_json(ruta, ["Externo"])
        contenido_externo = ruta.read_bytes()

    with pytest.raises(ConcurrenciaError):
        lista.agregar("Nuevo")

    assert not lista.contiene("Nuevo")
    assert not lista.contiene("Externo")
    assert lista.contiene("Anterior") is (cambio != "creacion")
    if contenido_externo is None:
        assert not ruta.exists()
    else:
        assert ruta.read_bytes() == contenido_externo


def test_repetir_exclusion_tras_eliminacion_externa_tambien_detecta_conflicto(
    tmp_path: Path,
) -> None:
    ruta = tmp_path / "exclusiones.json"
    guardar_json(ruta, ["Anterior"])
    lista = ListaExclusionJson(ruta)
    ruta.unlink()

    with pytest.raises(ConcurrenciaError):
        lista.agregar("ANTERIOR")

    assert lista.contiene("Anterior")
    assert not ruta.exists()


def test_corrupcion_posterior_a_carga_impide_sobrescritura(tmp_path: Path) -> None:
    ruta = tmp_path / "exclusiones.json"
    guardar_json(ruta, ["Anterior"])
    lista = ListaExclusionJson(ruta)
    ruta.write_bytes(b"[incompleto")

    with pytest.raises(EstructuraError):
        lista.agregar("Nuevo")

    assert lista.contiene("Anterior")
    assert not lista.contiene("Nuevo")
    assert ruta.read_bytes() == b"[incompleto"


def test_dos_sesiones_no_pierden_la_exclusion_que_guardo_la_primera(tmp_path: Path) -> None:
    ruta = tmp_path / "exclusiones.json"
    primera = ListaExclusionJson(ruta)
    segunda = ListaExclusionJson(ruta)
    primera.agregar("Primera")

    with pytest.raises(ConcurrenciaError):
        segunda.agregar("Segunda")

    assert leer_json(ruta) == ["Primera"]
    assert not segunda.contiene("Segunda")
    nueva_sesion = ListaExclusionJson(ruta)
    nueva_sesion.agregar("Segunda")
    assert leer_json(ruta) == ["Primera", "Segunda"]


def test_cambio_solo_de_formato_permite_agregar(tmp_path: Path) -> None:
    ruta = tmp_path / "exclusiones.json"
    guardar_json(ruta, ["Anterior"])
    lista = ListaExclusionJson(ruta)
    ruta.write_text('["Anterior"]', encoding="utf-8-sig")

    lista.agregar("Nuevo")

    assert leer_json(ruta) == ["Anterior", "Nuevo"]


def test_log_de_exito_no_incluye_nombres_ni_rutas(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ruta = tmp_path / "lista-privada.json"
    lista = ListaExclusionJson(ruta)

    with caplog.at_level(logging.INFO):
        lista.agregar("Nombre privado")

    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.INFO
    assert "guardada" in caplog.text
    assert "privad" not in caplog.text
    assert caplog.records[0].exc_info is None
