"""Integridad del informe, validación de resultados y seguridad del guardado."""

import logging
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from automatizacion_clientes.core.contratos import AlmacenResoluciones
from automatizacion_clientes.core.errores import EstructuraError, PersistenciaError, ValidacionError
from automatizacion_clientes.core.modelos import (
    CandidatoCoincidencia,
    ContactoIgnorado,
    ContactoPendiente,
    ContactoResuelto,
    ContactoSemanal,
    MotivoIgnorado,
    MotivoPendiente,
    ResolucionSemanal,
    UbicacionCliente,
    ViaCoincidencia,
)
from automatizacion_clientes.infra.almacen_resoluciones_json import AlmacenResolucionesJson
from automatizacion_clientes.infra.almacenamiento_json import ValorJson, guardar_json, leer_json


@pytest.fixture
def contacto_documento() -> dict[str, ValorJson]:
    return {
        "nombre_crudo": "  Nombre privado (IOL)  ",
        "ultima_fecha": "2026-11-05",
        "telefono": "+5491100000000",
        "es_grupo": False,
    }


@pytest.fixture
def ubicacion_documento() -> dict[str, ValorJson]:
    return {"hoja": "IOL", "fila": 3}


@pytest.fixture
def candidato_documento() -> dict[str, ValorJson]:
    return {
        "persona_id": "p0001",
        "nombre_canonico": "Nombre privado",
        "hojas": ["IOL", "Bull"],
        "puntuacion": 92.3076923076923,
        "via": "fuzzy",
    }


@pytest.fixture
def resuelto_documento(
    contacto_documento: dict[str, ValorJson], ubicacion_documento: dict[str, ValorJson]
) -> dict[str, ValorJson]:
    return {
        "tipo": "resuelto",
        "contacto": contacto_documento,
        "persona_id": "p0001",
        "ubicaciones": [ubicacion_documento, {"hoja": "IOL", "fila": 20}],
        "puntuacion": 100,
        "via": "telefono",
    }


@pytest.fixture
def pendiente_documento(
    contacto_documento: dict[str, ValorJson], candidato_documento: dict[str, ValorJson]
) -> dict[str, ValorJson]:
    return {
        "tipo": "pendiente",
        "contacto": contacto_documento,
        "motivo": "ambiguo",
        "candidatos": [candidato_documento],
    }


@pytest.fixture
def ignorado_documento(contacto_documento: dict[str, ValorJson]) -> dict[str, ValorJson]:
    return {"tipo": "ignorado", "contacto": contacto_documento, "motivo": "en_lista_negra"}


@pytest.fixture
def documento(
    resuelto_documento: dict[str, ValorJson],
    pendiente_documento: dict[str, ValorJson],
    ignorado_documento: dict[str, ValorJson],
) -> dict[str, ValorJson]:
    return {
        "version_esquema": 1,
        "resueltos": [resuelto_documento],
        "pendientes": [pendiente_documento],
        "ignorados": [ignorado_documento],
        "avisos": ["Aviso privado", "Aviso privado", ""],
    }


@pytest.fixture
def resolucion() -> ResolucionSemanal:
    contacto = ContactoSemanal(
        nombre_crudo="  Nombre privado (IOL)  ",
        ultima_fecha=date(2026, 11, 5),
        telefono="+5491100000000",
    )
    resuelto = ContactoResuelto(
        contacto=contacto,
        persona_id="p0042",
        ubicaciones=(UbicacionCliente(hoja="IOL", fila=3), UbicacionCliente(hoja="IOL", fila=20)),
        puntuacion=100.0,
        via=ViaCoincidencia.TELEFONO,
    )
    candidato = CandidatoCoincidencia(
        persona_id="p0007",
        nombre_canonico="Candidato privado",
        hojas=frozenset({"IOL", "Bull"}),
        puntuacion=85.71428571428571,
        via=ViaCoincidencia.FUZZY,
    )
    return ResolucionSemanal(
        resueltos=(resuelto, replace(resuelto, persona_id="p0008")),
        pendientes=(
            ContactoPendiente(
                contacto=contacto, motivo=MotivoPendiente.AMBIGUO, candidatos=(candidato,)
            ),
            ContactoPendiente(
                contacto=replace(contacto, nombre_crudo="Otro"), motivo=MotivoPendiente.SIN_MATCH
            ),
        ),
        ignorados=(
            ContactoIgnorado(
                contacto=replace(contacto, es_grupo=True), motivo=MotivoIgnorado.GRUPO
            ),
            ContactoIgnorado(contacto=contacto, motivo=MotivoIgnorado.EN_LISTA_NEGRA),
        ),
        avisos=("Aviso privado", "Aviso privado", ""),
    )


def test_constructor_no_accede_al_disco(tmp_path: Path, mocker: MockerFixture) -> None:
    ruta = tmp_path / "nueva" / "informe.json"
    lectura = mocker.patch.object(Path, "read_text", side_effect=PermissionError("bloqueado"))

    almacen: AlmacenResoluciones = AlmacenResolucionesJson(ruta)

    assert almacen is not None
    lectura.assert_not_called()
    assert not ruta.parent.exists()


def test_guardar_y_leer_preservan_informe_completo_y_cotitulares(
    tmp_path: Path, resolucion: ResolucionSemanal
) -> None:
    ruta = tmp_path / "nueva" / "informe.json"
    almacen: AlmacenResoluciones = AlmacenResolucionesJson(ruta)

    almacen.guardar(resolucion)
    recargada = almacen.leer()

    assert recargada == resolucion
    assert recargada.resueltos[0].contacto == recargada.resueltos[1].contacto
    assert recargada.resueltos[0].persona_id != recargada.resueltos[1].persona_id
    assert recargada.resueltos[0].ubicaciones == resolucion.resueltos[0].ubicaciones
    assert recargada.pendientes[0].candidatos[0].puntuacion == 85.71428571428571
    assert recargada.pendientes[1].candidatos == ()
    assert recargada.avisos == ("Aviso privado", "Aviso privado", "")
    assert list(ruta.parent.iterdir()) == [ruta]
    original = ruta.read_bytes()
    almacen.guardar(recargada)
    assert ruta.read_bytes() == original


def test_lee_documento_independiente_sin_modificarlo(
    tmp_path: Path, documento: dict[str, ValorJson]
) -> None:
    ruta = tmp_path / "informe.json"
    guardar_json(ruta, documento)
    original = ruta.read_bytes()

    resolucion = AlmacenResolucionesJson(ruta).leer()

    assert resolucion.resueltos[0].via is ViaCoincidencia.TELEFONO
    assert resolucion.resueltos[0].contacto.ultima_fecha == date(2026, 11, 5)
    assert isinstance(resolucion.resueltos[0].puntuacion, float)
    assert resolucion.pendientes[0].motivo is MotivoPendiente.AMBIGUO
    assert resolucion.pendientes[0].candidatos[0].hojas == frozenset({"IOL", "Bull"})
    assert resolucion.ignorados[0].motivo is MotivoIgnorado.EN_LISTA_NEGRA
    assert ruta.read_bytes() == original


def test_informe_vacio_es_valido_y_se_guarda_con_todas_las_colecciones(tmp_path: Path) -> None:
    ruta = tmp_path / "informe.json"
    almacen = AlmacenResolucionesJson(ruta)
    almacen.guardar(ResolucionSemanal())

    assert almacen.leer() == ResolucionSemanal()
    assert leer_json(ruta) == {
        "version_esquema": 1,
        "resueltos": [],
        "pendientes": [],
        "ignorados": [],
        "avisos": [],
    }


@pytest.mark.parametrize("via", list(ViaCoincidencia))
def test_conserva_todas_las_vias(
    tmp_path: Path, resolucion: ResolucionSemanal, via: ViaCoincidencia
) -> None:
    resultado = replace(resolucion.resueltos[0], via=via)
    informe = replace(resolucion, resueltos=(resultado,))
    almacen = AlmacenResolucionesJson(tmp_path / "informe.json")
    almacen.guardar(informe)

    assert almacen.leer() == informe


@pytest.mark.parametrize("motivo", list(MotivoPendiente))
def test_conserva_todos_los_motivos_pendientes(
    tmp_path: Path, resolucion: ResolucionSemanal, motivo: MotivoPendiente
) -> None:
    informe = replace(resolucion, pendientes=(replace(resolucion.pendientes[0], motivo=motivo),))
    almacen = AlmacenResolucionesJson(tmp_path / "informe.json")
    almacen.guardar(informe)

    assert almacen.leer() == informe


def test_contacto_identificado_solo_por_telefono_se_conserva(
    tmp_path: Path, resolucion: ResolucionSemanal
) -> None:
    resultado = resolucion.resueltos[0]
    resultado = replace(resultado, contacto=replace(resultado.contacto, nombre_crudo=""))
    informe = ResolucionSemanal(resueltos=(resultado,))
    almacen = AlmacenResolucionesJson(tmp_path / "informe.json")
    almacen.guardar(informe)

    assert almacen.leer() == informe


@pytest.mark.parametrize("contenido", [None, [], {}])
def test_contenido_incorrecto_no_equivale_a_informe_vacio(
    tmp_path: Path, contenido: ValorJson
) -> None:
    ruta = tmp_path / "informe.json"
    guardar_json(ruta, contenido)

    with pytest.raises(EstructuraError):
        AlmacenResolucionesJson(ruta).leer()


def test_informe_anterior_requiere_regeneracion_sin_transformarlo(tmp_path: Path) -> None:
    ruta = tmp_path / "informe.json"
    guardar_json(ruta, {"resueltos": [], "pendientes": [], "ignorados": []})
    original = ruta.read_bytes()

    with pytest.raises(EstructuraError, match="versión"):
        AlmacenResolucionesJson(ruta).leer()

    assert ruta.read_bytes() == original


@pytest.mark.parametrize("version", [True, 1.0, "1", None, 0, 2])
def test_version_requiere_entero_exacto_admitido(
    tmp_path: Path, documento: dict[str, ValorJson], version: ValorJson
) -> None:
    documento["version_esquema"] = version
    ruta = tmp_path / "informe.json"
    guardar_json(ruta, documento)

    with pytest.raises(EstructuraError):
        AlmacenResolucionesJson(ruta).leer()


@pytest.mark.parametrize("campo", ["resueltos", "pendientes", "ignorados", "avisos"])
def test_colecciones_obligatorias_sin_coercion(
    tmp_path: Path, documento: dict[str, ValorJson], campo: str
) -> None:
    documento[campo] = {}
    ruta = tmp_path / "informe.json"
    guardar_json(ruta, documento)

    with pytest.raises(EstructuraError):
        AlmacenResolucionesJson(ruta).leer()


@pytest.mark.parametrize("cambio", ["campo_ausente", "campo_desconocido", "tipo_incorrecto"])
def test_valida_estado_y_campos_de_los_resultados(
    tmp_path: Path,
    documento: dict[str, ValorJson],
    resuelto_documento: dict[str, ValorJson],
    cambio: str,
) -> None:
    if cambio == "campo_ausente":
        del resuelto_documento["persona_id"]
    elif cambio == "campo_desconocido":
        resuelto_documento["mensajes"] = []
    else:
        resuelto_documento["tipo"] = "pendiente"
    ruta = tmp_path / "informe.json"
    guardar_json(ruta, documento)

    with pytest.raises(EstructuraError):
        AlmacenResolucionesJson(ruta).leer()


@pytest.mark.parametrize("campo", ["via", "puntuacion"])
@pytest.mark.parametrize("valor", [None, True, "desconocido"])
def test_rechaza_enum_o_puntuacion_incompatible(
    tmp_path: Path,
    documento: dict[str, ValorJson],
    resuelto_documento: dict[str, ValorJson],
    campo: str,
    valor: ValorJson,
) -> None:
    resuelto_documento[campo] = valor
    ruta = tmp_path / "informe.json"
    guardar_json(ruta, documento)

    with pytest.raises(EstructuraError):
        AlmacenResolucionesJson(ruta).leer()


@pytest.mark.parametrize("puntuacion", [-1, 101, 10**400])
def test_rechaza_puntuaciones_fuera_de_escala_sin_desbordamientos_sin_traducir(
    tmp_path: Path,
    documento: dict[str, ValorJson],
    resuelto_documento: dict[str, ValorJson],
    puntuacion: int,
) -> None:
    resuelto_documento["puntuacion"] = puntuacion
    ruta = tmp_path / "informe.json"
    guardar_json(ruta, documento)

    with pytest.raises(ValidacionError):
        AlmacenResolucionesJson(ruta).leer()


@pytest.mark.parametrize("fecha", ["2026-02-30", "20261105", "2026-11-05T12:00:00"])
def test_fecha_del_contacto_es_de_calendario_y_canonica(
    tmp_path: Path,
    documento: dict[str, ValorJson],
    contacto_documento: dict[str, ValorJson],
    fecha: str,
) -> None:
    contacto_documento["ultima_fecha"] = fecha
    ruta = tmp_path / "informe.json"
    guardar_json(ruta, documento)

    with pytest.raises(ValidacionError):
        AlmacenResolucionesJson(ruta).leer()


@pytest.mark.parametrize(
    ("campo", "valor"), [("telefono", 123), ("es_grupo", "false"), ("nombre_crudo", 123)]
)
def test_contacto_requiere_tipos_estrictos(
    tmp_path: Path,
    documento: dict[str, ValorJson],
    contacto_documento: dict[str, ValorJson],
    campo: str,
    valor: ValorJson,
) -> None:
    contacto_documento[campo] = valor
    ruta = tmp_path / "informe.json"
    guardar_json(ruta, documento)

    with pytest.raises(EstructuraError):
        AlmacenResolucionesJson(ruta).leer()


@pytest.mark.parametrize("fila", [True, "3", 3.0])
def test_filas_requieren_enteros_estrictos(
    tmp_path: Path,
    documento: dict[str, ValorJson],
    ubicacion_documento: dict[str, ValorJson],
    fila: ValorJson,
) -> None:
    ubicacion_documento["fila"] = fila
    ruta = tmp_path / "informe.json"
    guardar_json(ruta, documento)

    with pytest.raises(EstructuraError):
        AlmacenResolucionesJson(ruta).leer()


@pytest.mark.parametrize("duplicado", ["ubicacion", "candidato", "hoja"])
def test_rechaza_duplicados_que_cambian_el_significado_del_resultado(
    tmp_path: Path,
    documento: dict[str, ValorJson],
    resuelto_documento: dict[str, ValorJson],
    ubicacion_documento: dict[str, ValorJson],
    pendiente_documento: dict[str, ValorJson],
    candidato_documento: dict[str, ValorJson],
    duplicado: str,
) -> None:
    if duplicado == "ubicacion":
        resuelto_documento["ubicaciones"] = [ubicacion_documento, ubicacion_documento]
    elif duplicado == "candidato":
        pendiente_documento["candidatos"] = [candidato_documento, candidato_documento]
    else:
        candidato_documento["hojas"] = ["IOL", "IOL"]
    ruta = tmp_path / "informe.json"
    guardar_json(ruta, documento)

    with pytest.raises(EstructuraError if duplicado == "hoja" else ValidacionError):
        AlmacenResolucionesJson(ruta).leer()


def test_archivo_ausente_es_error_y_no_crea_salida(tmp_path: Path) -> None:
    ruta = tmp_path / "nueva" / "informe.json"

    with pytest.raises(PersistenciaError) as capturada:
        AlmacenResolucionesJson(ruta).leer()

    assert isinstance(capturada.value.__cause__, FileNotFoundError)
    assert not ruta.parent.exists()


def test_cada_lectura_refleja_archivo_actual_y_rechaza_corrupcion(
    tmp_path: Path, resolucion: ResolucionSemanal
) -> None:
    ruta = tmp_path / "informe.json"
    almacen = AlmacenResolucionesJson(ruta)
    almacen.guardar(resolucion)
    primera = almacen.leer()
    almacen.guardar(ResolucionSemanal())
    assert almacen.leer() == ResolucionSemanal()
    assert primera == resolucion
    ruta.write_bytes(b"{incompleto")

    with pytest.raises(EstructuraError):
        almacen.leer()

    assert ruta.read_bytes() == b"{incompleto"


def test_fallo_de_acceso_preserva_causa_tecnica(tmp_path: Path, mocker: MockerFixture) -> None:
    causa = PermissionError("Detalle privado")
    mocker.patch.object(Path, "read_text", side_effect=causa)

    with pytest.raises(PersistenciaError) as capturada:
        AlmacenResolucionesJson(tmp_path / "informe.json").leer()

    assert capturada.value.__cause__ is causa
    assert "privado" not in str(capturada.value)


@pytest.mark.parametrize("archivo_previo", [True, False])
def test_fallo_de_guardado_conserva_informe_y_permite_reintentar(
    tmp_path: Path, resolucion: ResolucionSemanal, mocker: MockerFixture, archivo_previo: bool
) -> None:
    ruta = tmp_path / "informe.json"
    almacen = AlmacenResolucionesJson(ruta)
    if archivo_previo:
        almacen.guardar(resolucion)
    actualizado = replace(resolucion, avisos=("Nuevo aviso",))
    fallo = mocker.patch.object(Path, "replace", side_effect=PermissionError("bloqueado"))

    with pytest.raises(PersistenciaError):
        almacen.guardar(actualizado)

    if archivo_previo:
        assert almacen.leer() == resolucion
        assert list(tmp_path.iterdir()) == [ruta]
    else:
        assert list(tmp_path.iterdir()) == []
    mocker.stop(fallo)
    almacen.guardar(actualizado)
    assert almacen.leer() == actualizado


def test_logs_no_revelan_avisos_rutas_identidades_ni_telefonos(
    tmp_path: Path, resolucion: ResolucionSemanal, caplog: pytest.LogCaptureFixture
) -> None:
    almacen = AlmacenResolucionesJson(tmp_path / "informe-privado.json")

    with caplog.at_level(logging.INFO):
        almacen.guardar(resolucion)
        almacen.leer()

    assert len(caplog.records) == 2
    assert "2 resueltos, 2 pendientes, 2 ignorados y 3 avisos" in caplog.text
    for contenido in ("privad", "+5491100000000", "p0042", "p0007"):
        assert contenido not in caplog.text


def test_fallo_de_validacion_no_registra_informe_parcial(
    tmp_path: Path,
    documento: dict[str, ValorJson],
    ignorado_documento: dict[str, ValorJson],
    caplog: pytest.LogCaptureFixture,
) -> None:
    ignorado_documento["motivo"] = "Motivo privado desconocido"
    ruta = tmp_path / "informe.json"
    guardar_json(ruta, documento)

    with caplog.at_level(logging.INFO), pytest.raises(EstructuraError) as capturada:
        AlmacenResolucionesJson(ruta).leer()

    assert not caplog.records
    assert "privado" not in str(capturada.value)
