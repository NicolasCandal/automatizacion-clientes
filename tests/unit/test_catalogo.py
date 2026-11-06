"""Identidades estables, lectura coherente y política de caché del catálogo."""

import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from automatizacion_clientes.core.catalogo import construir_catalogo
from automatizacion_clientes.core.contratos import AlmacenCatalogo, LectorClientes, Reloj
from automatizacion_clientes.core.errores import (
    ConcurrenciaError,
    ConfiguracionError,
    EstructuraError,
    PersistenciaError,
    ValidacionError,
)
from automatizacion_clientes.core.modelos import CatalogoClientes, FilaCliente, HojaInfo
from automatizacion_clientes.services.catalogo import ServicioCatalogo

INSTANTE = datetime(2026, 11, 5, 18, 4, tzinfo=UTC)


@pytest.fixture
def hojas() -> tuple[HojaInfo, ...]:
    return (
        HojaInfo(
            nombre="IOL",
            sheet_id=0,
            col_nombre=1,
            primera_col_semana=3,
            ultima_col_semana=10,
            ultima_fila_con_cliente=20,
        ),
        HojaInfo(
            nombre="Bull",
            sheet_id=7,
            col_nombre=1,
            primera_col_semana=None,
            ultima_col_semana=None,
            ultima_fila_con_cliente=7,
        ),
        HojaInfo(
            nombre="PPI",
            sheet_id=2,
            col_nombre=1,
            primera_col_semana=None,
            ultima_col_semana=None,
            ultima_fila_con_cliente=2,
        ),
    )


@pytest.fixture
def clientes() -> dict[str, tuple[FilaCliente, ...]]:
    return {
        "IOL": (
            FilaCliente(fila=3, nombre_crudo="  Fedezu, Lidexe Bekaxo  "),
            FilaCliente(fila=10, nombre_crudo="Lidexe Bekaxo Fedezu"),
            FilaCliente(fila=20, nombre_crudo="RAXOZI GOGOTE Y/O RETUBO GEZIGE RATIXU"),
        ),
        "Bull": (
            FilaCliente(fila=3, nombre_crudo="LÍDEXE BEKAXO FÉDEZU"),
            FilaCliente(fila=7, nombre_crudo="MOPIVO BEMIBE (esposa Gosoru Fezixi)"),
        ),
        "PPI": (),
    }


def construir(
    hojas: tuple[HojaInfo, ...],
    clientes: dict[str, tuple[FilaCliente, ...]],
    anterior: CatalogoClientes | None = None,
) -> CatalogoClientes:
    return construir_catalogo(
        identificador_origen="origen_sintetico",
        revision="revision_1",
        generado_en=INSTANTE,
        hojas=hojas,
        clientes_por_hoja=clientes,
        anterior=anterior,
    )


@pytest.fixture
def catalogo(
    hojas: tuple[HojaInfo, ...], clientes: dict[str, tuple[FilaCliente, ...]]
) -> CatalogoClientes:
    return construir(hojas, clientes)


@dataclass
class EntornoCatalogo:
    lector: MagicMock
    almacen: MagicMock
    reloj: MagicMock

    def servicio(self, origen: str = "origen_sintetico") -> ServicioCatalogo:
        return ServicioCatalogo(
            lector_clientes=self.lector,
            almacen_catalogo=self.almacen,
            reloj=self.reloj,
            identificador_origen=origen,
        )


@pytest.fixture
def entorno(
    mocker: MockerFixture, hojas: tuple[HojaInfo, ...], clientes: dict[str, tuple[FilaCliente, ...]]
) -> EntornoCatalogo:
    entorno = EntornoCatalogo(
        lector=mocker.create_autospec(LectorClientes, instance=True, spec_set=True),
        almacen=mocker.create_autospec(AlmacenCatalogo, instance=True, spec_set=True),
        reloj=mocker.create_autospec(Reloj, instance=True, spec_set=True),
    )
    entorno.almacen.leer.return_value = None
    entorno.lector.revision.return_value = "revision_1"
    entorno.lector.listar_hojas.return_value = hojas
    entorno.lector.leer_clientes.side_effect = clientes.__getitem__
    entorno.reloj.ahora.return_value = INSTANTE
    return entorno


def test_construccion_conserva_todas_las_filas_variantes_y_cotitulares(
    catalogo: CatalogoClientes,
) -> None:
    assert len(catalogo.personas) == 5
    assert tuple(hoja.nombre for hoja in catalogo.hojas) == ("IOL", "Bull", "PPI")
    assert [persona.clave_orden for persona in catalogo.personas] == sorted(
        persona.clave_orden for persona in catalogo.personas
    )
    por_clave = {persona.clave_orden: persona for persona in catalogo.personas}
    fedezu = por_clave["bekaxo fedezu lidexe"]
    assert fedezu.nombre_canonico == "Fedezu, Lidexe Bekaxo"
    assert len(fedezu.titulares) == 3
    assert tuple((ubicacion.hoja, ubicacion.fila) for ubicacion in fedezu.ubicaciones) == (
        ("IOL", 3),
        ("IOL", 10),
        ("Bull", 3),
    )
    assert fedezu.apariciones[0].nombre_crudo == "  Fedezu, Lidexe Bekaxo  "
    assert por_clave["gogote raxozi"].ubicaciones == por_clave["gezige ratixu retubo"].ubicaciones
    assert por_clave["fezixi gosoru"].ubicaciones[0].fila == 7


def test_insertar_cliente_no_renumera_ids_anteriores(
    hojas: tuple[HojaInfo, ...],
    clientes: dict[str, tuple[FilaCliente, ...]],
    catalogo: CatalogoClientes,
) -> None:
    anterior = replace(
        catalogo,
        personas=tuple(
            replace(persona, persona_id=f"p{numero:04d}")
            for numero, persona in enumerate(catalogo.personas, start=1)
        ),
    )
    clientes["IOL"] += (FilaCliente(fila=17, nombre_crudo="AAA Nueva Persona"),)

    actualizado = construir(hojas, clientes, anterior)

    anteriores = {persona.clave_orden: persona.persona_id for persona in anterior.personas}
    actuales = {persona.clave_orden: persona.persona_id for persona in actualizado.personas}
    assert all(actuales[clave] == identificador for clave, identificador in anteriores.items())
    assert actuales["aaa nueva persona"].startswith("p_")
    assert actuales["aaa nueva persona"] not in anteriores.values()


def test_ids_nuevos_no_dependen_del_orden_ni_de_disponer_de_cache(
    hojas: tuple[HojaInfo, ...],
    clientes: dict[str, tuple[FilaCliente, ...]],
    catalogo: CatalogoClientes,
) -> None:
    invertidos = {hoja: tuple(reversed(filas)) for hoja, filas in clientes.items()}
    reconstruido = construir(tuple(reversed(hojas)), invertidos)
    assert {persona.clave_orden: persona.persona_id for persona in reconstruido.personas} == {
        persona.clave_orden: persona.persona_id for persona in catalogo.personas
    }


def test_no_reutiliza_id_reservado_para_otra_clave(
    hojas: tuple[HojaInfo, ...],
    clientes: dict[str, tuple[FilaCliente, ...]],
    catalogo: CatalogoClientes,
) -> None:
    anterior = replace(
        catalogo,
        personas=(replace(catalogo.personas[0], persona_id=catalogo.personas[1].persona_id),),
    )
    with pytest.raises(ValidacionError, match="reservada"):
        construir(hojas, clientes, anterior)


def test_ids_deterministas_separan_origenes_distintos(
    hojas: tuple[HojaInfo, ...],
    clientes: dict[str, tuple[FilaCliente, ...]],
    catalogo: CatalogoClientes,
) -> None:
    otro = construir_catalogo(
        identificador_origen="otro_origen",
        revision="revision_1",
        generado_en=INSTANTE,
        hojas=hojas,
        clientes_por_hoja=clientes,
    )

    assert {persona.persona_id for persona in otro.personas}.isdisjoint(
        persona.persona_id for persona in catalogo.personas
    )


def test_variantes_del_mismo_titular_en_una_celda_no_duplican_ubicaciones(
    hojas: tuple[HojaInfo, ...], clientes: dict[str, tuple[FilaCliente, ...]]
) -> None:
    clientes["IOL"] = (FilaCliente(fila=3, nombre_crudo="Axa Prueba Y/O Prueba Axa"),)
    clientes["Bull"] = ()
    catalogo = construir(hojas, clientes)
    assert len(catalogo.personas) == 1
    assert catalogo.personas[0].titulares == ("Axa Prueba", "Prueba Axa")
    assert len(catalogo.personas[0].ubicaciones) == 1


def test_titular_identico_en_varias_filas_conserva_todas_sus_apariciones(
    hojas: tuple[HojaInfo, ...], clientes: dict[str, tuple[FilaCliente, ...]]
) -> None:
    clientes["IOL"] = (
        FilaCliente(fila=3, nombre_crudo="Axa Prueba"),
        FilaCliente(fila=20, nombre_crudo="Axa Prueba"),
    )
    clientes["Bull"] = (FilaCliente(fila=7, nombre_crudo="Axa Prueba"),)

    catalogo = construir(hojas, clientes)

    assert len(catalogo.personas) == 1
    assert catalogo.personas[0].titulares == ("Axa Prueba",)
    assert len(catalogo.personas[0].apariciones) == 3


@pytest.mark.parametrize("cambio", ["hoja_ausente", "hoja_extra", "fila_duplicada", "fila_fuera"])
def test_rechaza_lectura_de_estructura_incoherente(
    hojas: tuple[HojaInfo, ...], clientes: dict[str, tuple[FilaCliente, ...]], cambio: str
) -> None:
    if cambio == "hoja_ausente":
        del clientes["PPI"]
    elif cambio == "hoja_extra":
        clientes["Otra"] = ()
    elif cambio == "fila_duplicada":
        clientes["IOL"] += (clientes["IOL"][0],)
    else:
        clientes["IOL"] += (FilaCliente(fila=21, nombre_crudo="Fuera"),)
    with pytest.raises(ValidacionError):
        construir(hojas, clientes)


@pytest.mark.parametrize("nombre", ["!!!", "(ruido)", "Axa Prueba Y/O !!!"])
def test_no_descarta_celdas_o_titulares_sin_texto_utilizable(
    hojas: tuple[HojaInfo, ...], clientes: dict[str, tuple[FilaCliente, ...]], nombre: str
) -> None:
    clientes["IOL"] = (FilaCliente(fila=3, nombre_crudo=nombre),)
    with pytest.raises(ValidacionError):
        construir(hojas, clientes)


def test_nucleo_rechaza_catalogo_anterior_de_otro_origen(
    hojas: tuple[HojaInfo, ...],
    clientes: dict[str, tuple[FilaCliente, ...]],
    catalogo: CatalogoClientes,
) -> None:
    with pytest.raises(ValidacionError, match="mismo origen"):
        construir(hojas, clientes, replace(catalogo, identificador_origen="otro"))


def test_constructor_no_ejecuta_dependencias(entorno: EntornoCatalogo) -> None:
    entorno.servicio()
    assert not entorno.lector.mock_calls
    assert not entorno.almacen.mock_calls
    assert not entorno.reloj.mock_calls


def test_identificador_vacio_se_rechaza_sin_lecturas(entorno: EntornoCatalogo) -> None:
    with pytest.raises(ConfiguracionError):
        entorno.servicio(" ")
    assert not entorno.lector.mock_calls


def test_simula_por_defecto_sin_persistir_y_no_consulta_marcas_o_encabezados(
    entorno: EntornoCatalogo, catalogo: CatalogoClientes
) -> None:
    assert entorno.servicio().ejecutar() == catalogo
    entorno.almacen.guardar.assert_not_called()
    entorno.lector.leer_marcas.assert_not_called()
    entorno.lector.ultima_semana.assert_not_called()
    assert entorno.lector.revision.call_count == 3
    entorno.reloj.ahora.assert_called_once()


def test_real_guarda_solo_catalogo_completo(
    entorno: EntornoCatalogo, catalogo: CatalogoClientes
) -> None:
    assert entorno.servicio().ejecutar(simulacion=False) == catalogo
    entorno.almacen.guardar.assert_called_once_with(catalogo)


def test_origen_sin_hojas_produce_un_catalogo_vacio_valido(entorno: EntornoCatalogo) -> None:
    entorno.lector.listar_hojas.return_value = ()

    catalogo = entorno.servicio().ejecutar(simulacion=False)

    assert catalogo.hojas == ()
    assert catalogo.personas == ()
    entorno.lector.leer_clientes.assert_not_called()
    entorno.almacen.guardar.assert_called_once_with(catalogo)


def test_cache_valida_se_reutiliza_sin_leer_hojas_ni_consultar_reloj(
    entorno: EntornoCatalogo, catalogo: CatalogoClientes
) -> None:
    entorno.almacen.leer.return_value = catalogo
    assert entorno.servicio().ejecutar(simulacion=False) is catalogo
    entorno.lector.listar_hojas.assert_not_called()
    entorno.lector.leer_clientes.assert_not_called()
    entorno.reloj.ahora.assert_not_called()
    entorno.almacen.guardar.assert_not_called()
    assert entorno.lector.revision.call_count == 2


def test_reconstruccion_forzada_conserva_ids_de_cache_valida(
    entorno: EntornoCatalogo, catalogo: CatalogoClientes
) -> None:
    anterior = replace(
        catalogo,
        personas=tuple(
            replace(persona, persona_id=f"p{numero:04d}")
            for numero, persona in enumerate(catalogo.personas, start=1)
        ),
    )
    entorno.almacen.leer.return_value = anterior
    reconstruido = entorno.servicio().ejecutar(simulacion=False, forzar=True)
    assert reconstruido == anterior
    entorno.lector.listar_hojas.assert_called_once()
    entorno.almacen.guardar.assert_called_once_with(reconstruido)


def test_revision_distinta_reconstruye_sin_perder_ids(
    entorno: EntornoCatalogo, catalogo: CatalogoClientes
) -> None:
    entorno.almacen.leer.return_value = replace(catalogo, revision="revision_anterior")
    assert entorno.servicio().ejecutar() == catalogo
    entorno.lector.listar_hojas.assert_called_once()


@pytest.mark.parametrize("forzar", [True, False])
def test_cache_de_otro_origen_no_se_sobrescribe_aunque_se_fuerce(
    entorno: EntornoCatalogo, catalogo: CatalogoClientes, forzar: bool
) -> None:
    entorno.almacen.leer.return_value = replace(catalogo, identificador_origen="otro")
    with pytest.raises(ConfiguracionError, match="otro origen"):
        entorno.servicio().ejecutar(simulacion=False, forzar=forzar)
    assert not entorno.lector.mock_calls
    entorno.almacen.guardar.assert_not_called()


@pytest.mark.parametrize("forzar", [True, False])
@pytest.mark.parametrize("error", [EstructuraError, ValidacionError])
def test_cache_invalida_solo_se_reconstruye_con_forzado_explicito(
    entorno: EntornoCatalogo,
    catalogo: CatalogoClientes,
    forzar: bool,
    error: type[EstructuraError] | type[ValidacionError],
) -> None:
    causa = error("Detalle privado")
    entorno.almacen.leer.side_effect = causa
    if forzar:
        assert entorno.servicio().ejecutar(simulacion=False, forzar=True) == catalogo
        entorno.almacen.guardar.assert_called_once_with(catalogo)
    else:
        with pytest.raises(error) as capturada:
            entorno.servicio().ejecutar(simulacion=False)
        assert capturada.value is causa
        assert not entorno.lector.mock_calls
        entorno.almacen.guardar.assert_not_called()


def test_forzar_no_oculta_fallos_de_acceso_a_cache(entorno: EntornoCatalogo) -> None:
    entorno.almacen.leer.side_effect = PersistenciaError("acceso denegado")
    with pytest.raises(PersistenciaError):
        entorno.servicio().ejecutar(simulacion=False, forzar=True)
    assert not entorno.lector.mock_calls
    entorno.almacen.guardar.assert_not_called()


def test_cambio_durante_comprobacion_de_cache_impide_reutilizarla(
    entorno: EntornoCatalogo, catalogo: CatalogoClientes
) -> None:
    entorno.almacen.leer.return_value = catalogo
    entorno.lector.revision.side_effect = ["revision_1", "revision_2"]
    with pytest.raises(ConcurrenciaError):
        entorno.servicio().ejecutar(simulacion=False)
    entorno.almacen.guardar.assert_not_called()


@pytest.mark.parametrize(
    "revisiones", [["revision_1", "revision_2"], ["revision_1", "revision_1", "revision_2"]]
)
def test_cambio_durante_lectura_o_construccion_impide_guardado(
    entorno: EntornoCatalogo, revisiones: list[str]
) -> None:
    entorno.lector.revision.side_effect = revisiones
    with pytest.raises(ConcurrenciaError):
        entorno.servicio().ejecutar(simulacion=False)
    entorno.almacen.guardar.assert_not_called()


@pytest.mark.parametrize("revision", ["", " "])
def test_revision_vacia_se_rechaza_antes_de_leer_clientes(
    entorno: EntornoCatalogo, revision: str
) -> None:
    entorno.lector.revision.return_value = revision
    with pytest.raises(EstructuraError):
        entorno.servicio().ejecutar(simulacion=False)
    entorno.lector.leer_clientes.assert_not_called()
    entorno.almacen.guardar.assert_not_called()


def test_lectura_parcial_fallida_no_guarda_catalogo(entorno: EntornoCatalogo) -> None:
    entorno.lector.leer_clientes.side_effect = [(), PersistenciaError("fallo de lectura")]
    with pytest.raises(PersistenciaError):
        entorno.servicio().ejecutar(simulacion=False)
    entorno.almacen.guardar.assert_not_called()
    entorno.reloj.ahora.assert_not_called()


def test_reloj_sin_zona_horaria_impide_guardado(entorno: EntornoCatalogo) -> None:
    entorno.reloj.ahora.return_value = INSTANTE.replace(tzinfo=None)
    with pytest.raises(ValidacionError):
        entorno.servicio().ejecutar(simulacion=False)
    entorno.almacen.guardar.assert_not_called()


def test_fallo_de_guardado_se_propaga_sin_reintentar_todo_el_flujo(
    entorno: EntornoCatalogo,
) -> None:
    causa = PersistenciaError("bloqueado")
    entorno.almacen.guardar.side_effect = causa
    with pytest.raises(PersistenciaError) as capturada:
        entorno.servicio().ejecutar(simulacion=False)
    assert capturada.value is causa
    entorno.almacen.guardar.assert_called_once()
    entorno.lector.listar_hojas.assert_called_once()


def test_logs_no_exponen_nombres_ids_revisiones_o_excepciones(
    entorno: EntornoCatalogo, caplog: pytest.LogCaptureFixture
) -> None:
    entorno.almacen.leer.side_effect = EstructuraError("Detalle secreto")
    with caplog.at_level(logging.INFO):
        entorno.servicio().ejecutar(forzar=True)
    assert len(caplog.records) == 2
    for fragmento in ("secreto", "Fedezu", "revision_1", "origen_sintetico", "p_"):
        assert fragmento not in caplog.text
    assert all(registro.exc_info is None for registro in caplog.records)
