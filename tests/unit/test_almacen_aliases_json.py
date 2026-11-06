"""Compatibilidad de aliases, conflictos y persistencia explícita de la sesión."""

import logging
from dataclasses import replace
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from automatizacion_clientes.core.contratos import AlmacenAliases
from automatizacion_clientes.core.errores import (
    ConcurrenciaError,
    EstructuraError,
    PersistenciaError,
    ValidacionError,
)
from automatizacion_clientes.core.modelos import AliasContacto, DestinoAlias
from automatizacion_clientes.infra.almacen_aliases_json import AlmacenAliasesJson
from automatizacion_clientes.infra.almacenamiento_json import ValorJson, guardar_json, leer_json


@pytest.fixture
def alias() -> AliasContacto:
    return AliasContacto(
        nombre_crudo="  Contacto privado (IOL)  ",
        telefono="+5491100000000",
        personas=(DestinoAlias(persona_id="p0001", hojas=frozenset({"IOL", "Bull"})),),
    )


@pytest.fixture
def entrada() -> dict[str, ValorJson]:
    return {
        "personas": [{"persona_id": "p0001", "hojas": ["IOL", "Bull"]}],
        "telefono": "+5491100000000",
    }


def test_ausencia_inicia_sesion_vacia_sin_crear_directorios(tmp_path: Path) -> None:
    ruta = tmp_path / "nueva" / "aliases.json"
    almacen: AlmacenAliases = AlmacenAliasesJson(ruta)

    assert almacen.buscar_por_nombre("inexistente") is None
    assert almacen.buscar_por_telefono("+5491100000000") == ()
    assert not ruta.parent.exists()


def test_lee_formato_historico_y_conserva_nombre_exacto(
    tmp_path: Path, alias: AliasContacto, entrada: dict[str, ValorJson]
) -> None:
    ruta = tmp_path / "aliases.json"
    guardar_json(ruta, {alias.nombre_crudo: entrada})
    contenido_previo = ruta.read_bytes()

    almacen: AlmacenAliases = AlmacenAliasesJson(ruta)

    assert almacen.buscar_por_nombre(alias.nombre_crudo) == alias
    assert almacen.buscar_por_nombre(alias.nombre_crudo.strip()) is None
    assert almacen.buscar_por_nombre(alias.nombre_crudo.lower()) is None
    assert almacen.buscar_por_telefono("+5491100000000") == (alias,)
    assert almacen.buscar_por_telefono("5491100000000") == ()
    assert almacen.buscar_por_telefono("") == ()
    assert ruta.read_bytes() == contenido_previo


def test_admite_entrada_historica_sin_telefono(
    tmp_path: Path, alias: AliasContacto, entrada: dict[str, ValorJson]
) -> None:
    ruta = tmp_path / "aliases.json"
    del entrada["telefono"]
    guardar_json(ruta, {alias.nombre_crudo: entrada})

    almacen = AlmacenAliasesJson(ruta)

    assert almacen.buscar_por_nombre(alias.nombre_crudo) == replace(alias, telefono=None)


def test_registrar_solo_modifica_memoria_y_guardar_conserva_formato(
    tmp_path: Path, alias: AliasContacto
) -> None:
    ruta = tmp_path / "nueva" / "aliases.json"
    almacen: AlmacenAliases = AlmacenAliasesJson(ruta)
    cotitular = DestinoAlias(persona_id="p0002", hojas=frozenset({"Balanz"}))
    alias = replace(alias, personas=(*alias.personas, cotitular))

    assert almacen.registrar(alias) is None
    assert almacen.buscar_por_nombre(alias.nombre_crudo) == alias
    assert not ruta.parent.exists()

    almacen.guardar()

    assert leer_json(ruta) == {
        alias.nombre_crudo: {
            "personas": [
                {"persona_id": "p0001", "hojas": ["Bull", "IOL"]},
                {"persona_id": "p0002", "hojas": ["Balanz"]},
            ],
            "telefono": "+5491100000000",
        }
    }
    assert AlmacenAliasesJson(ruta).buscar_por_nombre(alias.nombre_crudo) == alias
    assert list(ruta.parent.iterdir()) == [ruta]


def test_guardar_sesion_vacia_crea_objeto_json(tmp_path: Path) -> None:
    ruta = tmp_path / "aliases.json"

    AlmacenAliasesJson(ruta).guardar()

    assert leer_json(ruta) == {}


def test_alias_sin_nombre_se_conserva_por_telefono(tmp_path: Path, alias: AliasContacto) -> None:
    ruta = tmp_path / "aliases.json"
    alias = replace(alias, nombre_crudo="")
    almacen = AlmacenAliasesJson(ruta)
    almacen.registrar(alias)
    almacen.guardar()

    assert AlmacenAliasesJson(ruta).buscar_por_telefono(alias.telefono or "") == (alias,)


def test_telefono_compartido_devuelve_todos_los_destinos_en_orden_estable(
    tmp_path: Path, alias: AliasContacto
) -> None:
    ruta = tmp_path / "aliases.json"
    primero = replace(alias, nombre_crudo="Alias A")
    segundo = replace(
        alias,
        nombre_crudo="Alias B",
        personas=(DestinoAlias(persona_id="p0002", hojas=frozenset({"PPI"})),),
    )
    almacen = AlmacenAliasesJson(ruta)
    almacen.registrar(segundo)
    almacen.registrar(primero)
    almacen.registrar(replace(alias, nombre_crudo="Otro", telefono=None))

    assert almacen.buscar_por_telefono("+5491100000000") == (primero, segundo)
    almacen.guardar()
    assert AlmacenAliasesJson(ruta).buscar_por_telefono("+5491100000000") == (primero, segundo)


def test_registros_compatibles_son_idempotentes_aunque_cambie_el_orden(
    tmp_path: Path, alias: AliasContacto, caplog: pytest.LogCaptureFixture
) -> None:
    cotitular = DestinoAlias(persona_id="p0002", hojas=frozenset({"PPI"}))
    alias = replace(alias, personas=(*alias.personas, cotitular))
    almacen = AlmacenAliasesJson(tmp_path / "aliases.json")

    with caplog.at_level(logging.WARNING):
        assert almacen.registrar(alias) is None
        assert almacen.registrar(replace(alias, personas=tuple(reversed(alias.personas)))) is None
        assert almacen.registrar(replace(alias, telefono=None)) is None
        assert almacen.registrar(alias) is None

    assert almacen.buscar_por_nombre(alias.nombre_crudo) == alias
    assert almacen.buscar_por_telefono("+5491100000000") == (alias,)
    assert not caplog.records


def test_completar_telefono_no_modifica_destinos_y_persiste(
    tmp_path: Path, alias: AliasContacto
) -> None:
    ruta = tmp_path / "aliases.json"
    almacen = AlmacenAliasesJson(ruta)
    almacen.registrar(replace(alias, telefono=None))
    almacen.guardar()

    assert almacen.registrar(alias) is None
    assert almacen.buscar_por_telefono("+5491100000000") == (alias,)
    assert AlmacenAliasesJson(ruta).buscar_por_telefono("+5491100000000") == ()
    almacen.guardar()

    assert AlmacenAliasesJson(ruta).buscar_por_nombre(alias.nombre_crudo) == alias


@pytest.mark.parametrize("conflicto", ["persona", "hojas", "telefono"])
def test_conflicto_conserva_toda_la_entrada_y_no_expone_datos_personales(
    tmp_path: Path,
    alias: AliasContacto,
    conflicto: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ruta = tmp_path / "aliases.json"
    almacen = AlmacenAliasesJson(ruta)
    almacen.registrar(alias)
    almacen.guardar()
    contenido_previo = ruta.read_bytes()
    if conflicto == "persona":
        nuevo = replace(
            alias, personas=(DestinoAlias(persona_id="p0002", hojas=frozenset({"IOL"})),)
        )
    elif conflicto == "hojas":
        nuevo = replace(alias, personas=(replace(alias.personas[0], hojas=frozenset({"PPI"})),))
    else:
        nuevo = replace(alias, telefono="+5491199999999")

    with caplog.at_level(logging.WARNING):
        aviso = almacen.registrar(nuevo)

    assert aviso is not None
    assert "Se conservó" in aviso
    assert almacen.buscar_por_nombre(alias.nombre_crudo) == alias
    assert almacen.buscar_por_telefono("+5491100000000") == (alias,)
    assert almacen.buscar_por_telefono("+5491199999999") == ()
    assert len(caplog.records) == 1
    for dato_privado in (alias.nombre_crudo, "+5491100000000", "+5491199999999", "p0001", "p0002"):
        assert dato_privado not in aviso
        assert dato_privado not in caplog.text
    almacen.guardar()
    assert ruta.read_bytes() == contenido_previo


@pytest.mark.parametrize(
    "documento",
    [
        None,
        [],
        "objeto esperado",
        {"Nombre": None},
        {"Nombre": {}},
        {"Nombre": {"personas": [], "campo_desconocido": True}},
        {"Nombre": {"personas": "lista esperada"}},
        {"Nombre": {"personas": [], "telefono": 123}},
        {"Nombre": {"personas": [None]}},
        {"Nombre": {"personas": [{"persona_id": "p0001"}]}},
        {"Nombre": {"personas": [{"persona_id": "p0001", "hojas": ["IOL"], "fila": 3}]}},
        {"Nombre": {"personas": [{"persona_id": 1, "hojas": ["IOL"]}]}},
        {"Nombre": {"personas": [{"persona_id": "p0001", "hojas": "IOL"}]}},
        {"Nombre": {"personas": [{"persona_id": "p0001", "hojas": [42]}]}},
        {"Nombre": {"personas": [{"persona_id": "p0001", "hojas": ["IOL", "IOL"]}]}},
    ],
)
def test_rechaza_esquema_incorrecto_sin_reescribir(tmp_path: Path, documento: ValorJson) -> None:
    ruta = tmp_path / "aliases.json"
    guardar_json(ruta, documento)
    contenido_previo = ruta.read_bytes()

    with pytest.raises(EstructuraError):
        AlmacenAliasesJson(ruta)

    assert ruta.read_bytes() == contenido_previo


@pytest.mark.parametrize(
    "documento",
    [
        {"Nombre": {"personas": []}},
        {" ": {"personas": [{"persona_id": "p0001", "hojas": ["IOL"]}]}},
        {"Nombre": {"personas": [{"persona_id": " ", "hojas": ["IOL"]}]}},
        {"Nombre": {"personas": [{"persona_id": "p0001", "hojas": []}]}},
        {"Nombre": {"personas": [{"persona_id": "p0001", "hojas": [" "]}]}},
        {
            "Nombre": {
                "personas": [{"persona_id": "p0001", "hojas": ["IOL"]}],
                "telefono": " ",
            }
        },
        {
            "Nombre": {
                "personas": [
                    {"persona_id": "p0001", "hojas": ["IOL"]},
                    {"persona_id": "p0001", "hojas": ["Bull"]},
                ]
            }
        },
    ],
)
def test_rechaza_identidades_invalidas_sin_reescribir(tmp_path: Path, documento: ValorJson) -> None:
    ruta = tmp_path / "aliases.json"
    guardar_json(ruta, documento)
    contenido_previo = ruta.read_bytes()

    with pytest.raises(ValidacionError):
        AlmacenAliasesJson(ruta)

    assert ruta.read_bytes() == contenido_previo


def test_json_corrupto_no_inicia_almacen_vacio(tmp_path: Path) -> None:
    ruta = tmp_path / "aliases.json"
    ruta.write_bytes(b"{incompleto")

    with pytest.raises(EstructuraError):
        AlmacenAliasesJson(ruta)

    assert ruta.read_bytes() == b"{incompleto"


def test_fallo_de_acceso_no_se_confunde_con_archivo_ausente(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    causa = PermissionError("acceso denegado")
    mocker.patch.object(Path, "read_text", side_effect=causa)

    with pytest.raises(PersistenciaError) as capturada:
        AlmacenAliasesJson(tmp_path / "aliases.json")

    assert capturada.value.__cause__ is causa


@pytest.mark.parametrize("archivo_previo", [True, False])
def test_fallo_de_guardado_conserva_cambios_para_reintento_explicito(
    tmp_path: Path, alias: AliasContacto, mocker: MockerFixture, archivo_previo: bool
) -> None:
    ruta = tmp_path / "aliases.json"
    if archivo_previo:
        guardar_json(ruta, {})
    almacen = AlmacenAliasesJson(ruta)
    almacen.registrar(alias)
    reemplazo = mocker.patch.object(Path, "replace", side_effect=PermissionError("bloqueado"))

    with pytest.raises(PersistenciaError):
        almacen.guardar()

    assert almacen.buscar_por_nombre(alias.nombre_crudo) == alias
    if archivo_previo:
        assert leer_json(ruta) == {}
    else:
        assert not ruta.exists()
    mocker.stop(reemplazo)
    almacen.guardar()
    assert AlmacenAliasesJson(ruta).buscar_por_nombre(alias.nombre_crudo) == alias


@pytest.mark.parametrize("cambio_externo", ["creacion", "modificacion", "eliminacion"])
def test_detecta_cambios_externos_sin_sobrescribir_ni_descartar_la_sesion(
    tmp_path: Path, alias: AliasContacto, cambio_externo: str
) -> None:
    ruta = tmp_path / "aliases.json"
    if cambio_externo != "creacion":
        guardar_json(ruta, {})
    almacen = AlmacenAliasesJson(ruta)
    almacen.registrar(alias)
    if cambio_externo == "eliminacion":
        ruta.unlink()
        contenido_externo = None
    else:
        guardar_json(
            ruta,
            {"Alias externo": {"personas": [{"persona_id": "p0002", "hojas": ["PPI"]}]}},
        )
        contenido_externo = ruta.read_bytes()

    with pytest.raises(ConcurrenciaError):
        almacen.guardar()

    assert almacen.buscar_por_nombre(alias.nombre_crudo) == alias
    if contenido_externo is None:
        assert not ruta.exists()
    else:
        assert ruta.read_bytes() == contenido_externo


def test_dos_sesiones_no_pierden_el_alias_que_guardo_la_primera(
    tmp_path: Path, alias: AliasContacto
) -> None:
    ruta = tmp_path / "aliases.json"
    primera = AlmacenAliasesJson(ruta)
    segunda = AlmacenAliasesJson(ruta)
    primera.registrar(alias)
    segunda.registrar(replace(alias, nombre_crudo="Otro contacto"))
    primera.guardar()

    with pytest.raises(ConcurrenciaError):
        segunda.guardar()

    assert AlmacenAliasesJson(ruta).buscar_por_nombre(alias.nombre_crudo) == alias
    assert AlmacenAliasesJson(ruta).buscar_por_nombre("Otro contacto") is None


def test_corrupcion_despues_de_cargar_no_se_sobrescribe(
    tmp_path: Path, alias: AliasContacto
) -> None:
    ruta = tmp_path / "aliases.json"
    guardar_json(ruta, {})
    almacen = AlmacenAliasesJson(ruta)
    almacen.registrar(alias)
    ruta.write_bytes(b"{incompleto")

    with pytest.raises(EstructuraError):
        almacen.guardar()

    assert ruta.read_bytes() == b"{incompleto"
    assert almacen.buscar_por_nombre(alias.nombre_crudo) == alias


def test_cambio_de_formato_sin_cambios_de_identidad_no_impide_guardar(
    tmp_path: Path, alias: AliasContacto, entrada: dict[str, ValorJson]
) -> None:
    ruta = tmp_path / "aliases.json"
    guardar_json(ruta, {alias.nombre_crudo: entrada})
    almacen = AlmacenAliasesJson(ruta)
    ruta.write_text(ruta.read_text(encoding="utf-8").replace("\n", ""), encoding="utf-8")

    almacen.guardar()

    assert AlmacenAliasesJson(ruta).buscar_por_nombre(alias.nombre_crudo) == alias
