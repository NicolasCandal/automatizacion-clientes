"""Catálogos versionados, integridad referencial y recuperación de cachés."""

import logging
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from automatizacion_clientes.core.contratos import AlmacenCatalogo
from automatizacion_clientes.core.errores import EstructuraError, PersistenciaError, ValidacionError
from automatizacion_clientes.core.modelos import (
    AparicionCliente,
    CatalogoClientes,
    HojaInfo,
    PersonaCliente,
    UbicacionCliente,
)
from automatizacion_clientes.infra.almacen_catalogo_json import AlmacenCatalogoJson
from automatizacion_clientes.infra.almacenamiento_json import ValorJson, guardar_json, leer_json


@pytest.fixture
def aparicion_documento() -> dict[str, ValorJson]:
    return {"hoja": "IOL", "fila": 3, "nombre_crudo": "Axa Prueba"}


@pytest.fixture
def persona_documento(aparicion_documento: dict[str, ValorJson]) -> dict[str, ValorJson]:
    return {
        "persona_id": "p0042",
        "clave_orden": "axa prueba",
        "nombre_canonico": "Axa Prueba",
        "titulares": ["Axa Prueba"],
        "apariciones": [aparicion_documento],
    }


@pytest.fixture
def hoja_documento() -> dict[str, ValorJson]:
    return {
        "nombre": "IOL",
        "sheet_id": 0,
        "col_nombre": 1,
        "primera_col_semana": 3,
        "ultima_col_semana": 10,
        "ultima_fila_con_cliente": 80,
    }


@pytest.fixture
def documento(
    hoja_documento: dict[str, ValorJson], persona_documento: dict[str, ValorJson]
) -> dict[str, ValorJson]:
    return {
        "version_esquema": 1,
        "identificador_origen": "origen_sintetico",
        "revision": "revision_1",
        "generado_en": "2026-11-05T18:04:00.123456-03:00",
        "hojas": [hoja_documento],
        "personas": [persona_documento],
    }


@pytest.fixture
def catalogo() -> CatalogoClientes:
    compartida = AparicionCliente(
        ubicacion=UbicacionCliente(hoja="IOL", fila=3), nombre_crudo="Áxa Prueba Y/O Notuka Ejemplo"
    )
    return CatalogoClientes(
        identificador_origen="origen privado",
        revision="revision privada",
        generado_en=datetime.fromisoformat("2026-11-05T18:04:00.123456-03:00"),
        hojas=(
            HojaInfo(
                nombre="Bull",
                sheet_id=7,
                col_nombre=1,
                primera_col_semana=None,
                ultima_col_semana=None,
                ultima_fila_con_cliente=9,
            ),
            HojaInfo(
                nombre="IOL",
                sheet_id=0,
                col_nombre=1,
                primera_col_semana=3,
                ultima_col_semana=10,
                ultima_fila_con_cliente=80,
            ),
        ),
        personas=(
            PersonaCliente(
                persona_id="p0042",
                clave_orden="axa prueba",
                nombre_canonico="Áxa Prueba",
                titulares=("Áxa Prueba", "Prueba, Axa"),
                apariciones=(
                    compartida,
                    AparicionCliente(
                        ubicacion=UbicacionCliente(hoja="IOL", fila=80),
                        nombre_crudo="  Prueba, Axa  ",
                    ),
                    AparicionCliente(
                        ubicacion=UbicacionCliente(hoja="Bull", fila=9), nombre_crudo="Áxa Prueba"
                    ),
                ),
            ),
            PersonaCliente(
                persona_id="p0007",
                clave_orden="ejemplo notuka",
                nombre_canonico="Notuka Ejemplo",
                titulares=("Notuka Ejemplo",),
                apariciones=(compartida,),
            ),
        ),
    )


def test_constructor_no_accede_al_disco(tmp_path: Path, mocker: MockerFixture) -> None:
    ruta = tmp_path / "nueva" / "catalogo.json"
    lectura = mocker.patch.object(Path, "read_text", side_effect=PermissionError("bloqueado"))

    almacen: AlmacenCatalogo = AlmacenCatalogoJson(ruta)

    assert almacen is not None
    lectura.assert_not_called()
    assert not ruta.parent.exists()


def test_ausencia_devuelve_none_sin_crear_directorios(tmp_path: Path) -> None:
    ruta = tmp_path / "nueva" / "catalogo.json"
    almacen: AlmacenCatalogo = AlmacenCatalogoJson(ruta)

    assert almacen.leer() is None
    assert not ruta.parent.exists()


def test_guardar_y_leer_preservan_todos_los_datos_orden_y_cotitulares(
    tmp_path: Path, catalogo: CatalogoClientes
) -> None:
    ruta = tmp_path / "nueva" / "catalogo.json"
    almacen: AlmacenCatalogo = AlmacenCatalogoJson(ruta)

    almacen.guardar(catalogo)
    recargado = almacen.leer()

    assert recargado == catalogo
    assert recargado is not None
    assert tuple(hoja.nombre for hoja in recargado.hojas) == ("Bull", "IOL")
    assert tuple(persona.persona_id for persona in recargado.personas) == ("p0042", "p0007")
    assert recargado.personas[0].ubicaciones == (
        UbicacionCliente(hoja="IOL", fila=3),
        UbicacionCliente(hoja="IOL", fila=80),
        UbicacionCliente(hoja="Bull", fila=9),
    )
    assert recargado.personas[0].ubicaciones[0] == recargado.personas[1].ubicaciones[0]
    assert recargado.generado_en.microsecond == 123456
    assert recargado.generado_en.utcoffset() == catalogo.generado_en.utcoffset()
    assert list(ruta.parent.iterdir()) == [ruta]
    assert "Áxa Prueba" in ruta.read_text(encoding="utf-8")


def test_documento_independiente_se_convierte_sin_reescribir(
    tmp_path: Path, documento: dict[str, ValorJson]
) -> None:
    ruta = tmp_path / "catalogo.json"
    guardar_json(ruta, documento)
    original = ruta.read_bytes()

    catalogo = AlmacenCatalogoJson(ruta).leer()

    assert catalogo is not None
    assert catalogo.identificador_origen == "origen_sintetico"
    assert catalogo.personas[0].persona_id == "p0042"
    assert catalogo.personas[0].titulares == ("Axa Prueba",)
    assert catalogo.hojas[0].ultima_fila_con_cliente == 80
    assert ruta.read_bytes() == original


def test_catalogo_vacio_es_distinto_de_archivo_ausente(
    tmp_path: Path, catalogo: CatalogoClientes
) -> None:
    ruta = tmp_path / "catalogo.json"
    vacio = replace(catalogo, hojas=(), personas=())
    almacen = AlmacenCatalogoJson(ruta)
    almacen.guardar(vacio)

    assert almacen.leer() == vacio


def test_guardado_repetido_es_reproducible_y_no_regenera_ids_o_fechas(
    tmp_path: Path, catalogo: CatalogoClientes
) -> None:
    ruta = tmp_path / "catalogo.json"
    almacen = AlmacenCatalogoJson(ruta)
    almacen.guardar(catalogo)
    original = ruta.read_bytes()
    almacen.guardar(catalogo)

    assert ruta.read_bytes() == original
    assert almacen.leer() == catalogo


def test_guardar_permite_reemplazar_cache_corrupta_tras_reconstruccion(
    tmp_path: Path, catalogo: CatalogoClientes
) -> None:
    ruta = tmp_path / "catalogo.json"
    ruta.write_bytes(b"{corrupto")

    AlmacenCatalogoJson(ruta).guardar(catalogo)

    assert AlmacenCatalogoJson(ruta).leer() == catalogo


@pytest.mark.parametrize("contenido", [None, [], {}])
def test_documento_incorrecto_no_equivale_a_archivo_ausente(
    tmp_path: Path, contenido: ValorJson
) -> None:
    ruta = tmp_path / "catalogo.json"
    guardar_json(ruta, contenido)

    with pytest.raises(EstructuraError):
        AlmacenCatalogoJson(ruta).leer()


def test_catalogo_historico_requiere_reconstruccion_sin_modificarlo(tmp_path: Path) -> None:
    ruta = tmp_path / "catalogo.json"
    guardar_json(
        ruta,
        {
            "spreadsheet_id": "origen_anterior",
            "revision": "revision_1",
            "generado_en": "2026-11-05T18:04:00-03:00",
            "hojas": {"IOL": {"filas_cliente": 2}},
            "personas": [],
        },
    )
    original = ruta.read_bytes()

    with pytest.raises(EstructuraError, match="Reconstrúyalo"):
        AlmacenCatalogoJson(ruta).leer()

    assert ruta.read_bytes() == original


@pytest.mark.parametrize("version", [None, True, 1.0, "1", 0, 2])
def test_version_requiere_entero_exacto_admitido(
    tmp_path: Path, documento: dict[str, ValorJson], version: ValorJson
) -> None:
    ruta = tmp_path / "catalogo.json"
    documento["version_esquema"] = version
    guardar_json(ruta, documento)

    with pytest.raises(EstructuraError, match="versión"):
        AlmacenCatalogoJson(ruta).leer()


@pytest.mark.parametrize(
    ("nivel", "campo"),
    [
        ("catalogo", "revision"),
        ("hoja", "sheet_id"),
        ("persona", "persona_id"),
        ("aparicion", "fila"),
    ],
)
@pytest.mark.parametrize("cambio", ["ausente", "desconocido"])
def test_rechaza_campos_ausentes_o_desconocidos_en_cualquier_nivel(
    tmp_path: Path,
    documento: dict[str, ValorJson],
    hoja_documento: dict[str, ValorJson],
    persona_documento: dict[str, ValorJson],
    aparicion_documento: dict[str, ValorJson],
    nivel: str,
    campo: str,
    cambio: str,
) -> None:
    entradas = {
        "catalogo": documento,
        "hoja": hoja_documento,
        "persona": persona_documento,
        "aparicion": aparicion_documento,
    }
    if cambio == "ausente":
        del entradas[nivel][campo]
    else:
        entradas[nivel]["campo_desconocido"] = "valor privado"
    ruta = tmp_path / "catalogo.json"
    guardar_json(ruta, documento)
    original = ruta.read_bytes()

    with pytest.raises(EstructuraError):
        AlmacenCatalogoJson(ruta).leer()

    assert ruta.read_bytes() == original


@pytest.mark.parametrize("campo", ["hojas", "personas"])
def test_colecciones_del_catalogo_requieren_listas(
    tmp_path: Path, documento: dict[str, ValorJson], campo: str
) -> None:
    ruta = tmp_path / "catalogo.json"
    documento[campo] = {}
    guardar_json(ruta, documento)

    with pytest.raises(EstructuraError):
        AlmacenCatalogoJson(ruta).leer()


@pytest.mark.parametrize("campo", ["identificador_origen", "revision", "generado_en"])
def test_campos_de_texto_no_se_coercionan(
    tmp_path: Path, documento: dict[str, ValorJson], campo: str
) -> None:
    ruta = tmp_path / "catalogo.json"
    documento[campo] = 123
    guardar_json(ruta, documento)

    with pytest.raises(EstructuraError):
        AlmacenCatalogoJson(ruta).leer()


@pytest.mark.parametrize(
    ("campo", "valor"),
    [
        ("sheet_id", True),
        ("col_nombre", "1"),
        ("ultima_fila_con_cliente", 80.0),
        ("primera_col_semana", "3"),
        ("ultima_col_semana", False),
    ],
)
def test_indices_requieren_enteros_sin_coercion(
    tmp_path: Path,
    documento: dict[str, ValorJson],
    hoja_documento: dict[str, ValorJson],
    campo: str,
    valor: ValorJson,
) -> None:
    ruta = tmp_path / "catalogo.json"
    hoja_documento[campo] = valor
    guardar_json(ruta, documento)

    with pytest.raises(EstructuraError):
        AlmacenCatalogoJson(ruta).leer()


@pytest.mark.parametrize("campo", ["titulares", "apariciones"])
def test_colecciones_de_persona_requieren_listas(
    tmp_path: Path,
    documento: dict[str, ValorJson],
    persona_documento: dict[str, ValorJson],
    campo: str,
) -> None:
    ruta = tmp_path / "catalogo.json"
    persona_documento[campo] = "lista esperada"
    guardar_json(ruta, documento)

    with pytest.raises(EstructuraError):
        AlmacenCatalogoJson(ruta).leer()


@pytest.mark.parametrize("coleccion", ["hojas", "personas"])
def test_elementos_de_colecciones_requieren_objetos(
    tmp_path: Path, documento: dict[str, ValorJson], coleccion: str
) -> None:
    ruta = tmp_path / "catalogo.json"
    documento[coleccion] = [None]
    guardar_json(ruta, documento)

    with pytest.raises(EstructuraError):
        AlmacenCatalogoJson(ruta).leer()


@pytest.mark.parametrize("instante", ["fecha inválida", "2026-11-05T18:04:00"])
def test_fecha_de_generacion_requiere_instante_valido_con_zona(
    tmp_path: Path, documento: dict[str, ValorJson], instante: str
) -> None:
    ruta = tmp_path / "catalogo.json"
    documento["generado_en"] = instante
    guardar_json(ruta, documento)

    with pytest.raises(ValidacionError):
        AlmacenCatalogoJson(ruta).leer()


@pytest.mark.parametrize(
    "relacion", ["persona", "clave", "hoja", "sheet_id", "ubicacion", "titular"]
)
def test_rechaza_identidades_y_relaciones_duplicadas(
    tmp_path: Path,
    documento: dict[str, ValorJson],
    hoja_documento: dict[str, ValorJson],
    persona_documento: dict[str, ValorJson],
    aparicion_documento: dict[str, ValorJson],
    relacion: str,
) -> None:
    if relacion == "persona":
        documento["personas"] = [
            persona_documento,
            {**persona_documento, "clave_orden": "otra clave"},
        ]
    elif relacion == "clave":
        documento["personas"] = [persona_documento, {**persona_documento, "persona_id": "p0007"}]
    elif relacion == "hoja":
        documento["hojas"] = [hoja_documento, {**hoja_documento, "sheet_id": 7}]
    elif relacion == "sheet_id":
        documento["hojas"] = [hoja_documento, {**hoja_documento, "nombre": "Bull"}]
    elif relacion == "ubicacion":
        persona_documento["apariciones"] = [aparicion_documento, aparicion_documento]
    else:
        persona_documento["titulares"] = ["Axa Prueba", "Axa Prueba"]
    ruta = tmp_path / "catalogo.json"
    guardar_json(ruta, documento)

    with pytest.raises(ValidacionError):
        AlmacenCatalogoJson(ruta).leer()


@pytest.mark.parametrize("referencia", ["hoja_ausente", "fila_fuera_del_limite"])
def test_referencias_de_apariciones_deben_corresponder_a_la_estructura(
    tmp_path: Path,
    documento: dict[str, ValorJson],
    aparicion_documento: dict[str, ValorJson],
    referencia: str,
) -> None:
    aparicion_documento["hoja" if referencia == "hoja_ausente" else "fila"] = (
        "Desconocida" if referencia == "hoja_ausente" else 81
    )
    ruta = tmp_path / "catalogo.json"
    guardar_json(ruta, documento)

    with pytest.raises(ValidacionError):
        AlmacenCatalogoJson(ruta).leer()


def test_cada_lectura_refleja_archivo_actual_sin_ocultar_corrupcion(
    tmp_path: Path, catalogo: CatalogoClientes
) -> None:
    ruta = tmp_path / "catalogo.json"
    almacen = AlmacenCatalogoJson(ruta)
    almacen.guardar(catalogo)
    primera = almacen.leer()
    actualizado = replace(catalogo, revision="revision_2")
    AlmacenCatalogoJson(ruta).guardar(actualizado)

    assert almacen.leer() == actualizado
    assert primera == catalogo
    ruta.write_bytes(b"{corrupto")
    with pytest.raises(EstructuraError):
        almacen.leer()
    assert ruta.read_bytes() == b"{corrupto"


def test_fallo_de_lectura_no_se_confunde_con_ausencia(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    causa = PermissionError("detalle privado")
    mocker.patch.object(Path, "read_text", side_effect=causa)

    with pytest.raises(PersistenciaError) as capturada:
        AlmacenCatalogoJson(tmp_path / "catalogo.json").leer()

    assert capturada.value.__cause__ is causa
    assert "privado" not in str(capturada.value)


@pytest.mark.parametrize("archivo_previo", [True, False])
def test_fallo_de_guardado_conserva_cache_y_permite_reintentar(
    tmp_path: Path, catalogo: CatalogoClientes, mocker: MockerFixture, archivo_previo: bool
) -> None:
    ruta = tmp_path / "catalogo.json"
    almacen = AlmacenCatalogoJson(ruta)
    if archivo_previo:
        almacen.guardar(catalogo)
    actualizado = replace(catalogo, revision="revision_2")
    causa = PermissionError("bloqueado")
    reemplazo = mocker.patch.object(Path, "replace", side_effect=causa)

    with pytest.raises(PersistenciaError) as capturada:
        almacen.guardar(actualizado)

    assert capturada.value.__cause__ is causa
    if archivo_previo:
        assert almacen.leer() == catalogo
        assert list(tmp_path.iterdir()) == [ruta]
    else:
        assert almacen.leer() is None
        assert list(tmp_path.iterdir()) == []
    mocker.stop(reemplazo)
    almacen.guardar(actualizado)
    assert almacen.leer() == actualizado


def test_esquema_de_salida_es_explicito_y_versionado(
    tmp_path: Path, catalogo: CatalogoClientes
) -> None:
    ruta = tmp_path / "catalogo.json"
    AlmacenCatalogoJson(ruta).guardar(catalogo)
    documento = leer_json(ruta)

    assert isinstance(documento, dict)
    assert documento["version_esquema"] == 1
    assert set(documento) == {
        "version_esquema",
        "identificador_origen",
        "revision",
        "generado_en",
        "hojas",
        "personas",
    }


def test_logs_solo_incluyen_cantidades_sin_rutas_ni_identidades(
    tmp_path: Path, catalogo: CatalogoClientes, caplog: pytest.LogCaptureFixture
) -> None:
    almacen = AlmacenCatalogoJson(tmp_path / "catalogo-privado.json")

    with caplog.at_level(logging.INFO):
        almacen.guardar(catalogo)
        almacen.leer()

    assert len(caplog.records) == 2
    assert all("2 personas y 2 hojas" in registro.message for registro in caplog.records)
    for identidad in ("privad", "Áxa", "p0042", "Notuka"):
        assert identidad not in caplog.text
