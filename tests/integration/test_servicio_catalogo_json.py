"""Caché real, identidades persistidas y resolución después de mover filas."""

from dataclasses import replace
from datetime import UTC
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from automatizacion_clientes.core.contratos import LectorClientes
from automatizacion_clientes.core.errores import EstructuraError
from automatizacion_clientes.core.modelos import (
    AliasContacto,
    DestinoAlias,
    FilaCliente,
    HojaInfo,
    UbicacionCliente,
    ViaCoincidencia,
)
from automatizacion_clientes.infra.almacen_aliases_json import AlmacenAliasesJson
from automatizacion_clientes.infra.almacen_catalogo_json import AlmacenCatalogoJson
from automatizacion_clientes.infra.almacen_resoluciones_json import AlmacenResolucionesJson
from automatizacion_clientes.infra.almacenamiento_json import guardar_json
from automatizacion_clientes.infra.comparador_rapidfuzz import ComparadorRapidFuzz
from automatizacion_clientes.infra.lector_contactos_json import LectorContactosJson
from automatizacion_clientes.infra.lista_exclusion_json import ListaExclusionJson
from automatizacion_clientes.infra.reloj_sistema import RelojSistema
from automatizacion_clientes.services.catalogo import ServicioCatalogo
from automatizacion_clientes.services.coincidencias import ServicioCoincidencias


@pytest.fixture
def lector(mocker: MockerFixture) -> MagicMock:
    lector = mocker.create_autospec(LectorClientes, instance=True, spec_set=True)
    lector.revision.return_value = "revision_1"
    lector.listar_hojas.return_value = (
        HojaInfo(
            nombre="IOL",
            sheet_id=0,
            col_nombre=1,
            primera_col_semana=None,
            ultima_col_semana=None,
            ultima_fila_con_cliente=3,
        ),
    )
    lector.leer_clientes.return_value = (FilaCliente(fila=3, nombre_crudo="Axa Prueba"),)
    return lector


def test_construccion_persistida_conserva_alias_al_insertar_cliente_y_mover_fila(
    tmp_path: Path, lector: MagicMock
) -> None:
    ruta_catalogo = tmp_path / "estado" / "catalogo.json"
    almacen = AlmacenCatalogoJson(ruta_catalogo)
    servicio = ServicioCatalogo(
        lector_clientes=lector,
        almacen_catalogo=almacen,
        reloj=RelojSistema(),
        identificador_origen="origen_sintetico",
    )
    simulado = servicio.ejecutar()
    assert not ruta_catalogo.parent.exists()
    assert simulado.generado_en.tzinfo is UTC
    primero = servicio.ejecutar(simulacion=False)
    assert primero.personas == simulado.personas
    assert almacen.leer() == primero
    contenido_cache = ruta_catalogo.read_bytes()
    assert servicio.ejecutar(simulacion=False) == primero
    assert ruta_catalogo.read_bytes() == contenido_cache
    persona_id = primero.personas[0].persona_id
    ruta_aliases = tmp_path / "estado" / "aliases.json"
    aliases = AlmacenAliasesJson(ruta_aliases)
    aliases.registrar(
        AliasContacto(
            nombre_crudo="Apodo conocido",
            personas=(DestinoAlias(persona_id=persona_id, hojas=frozenset({"IOL"})),),
        )
    )
    aliases.guardar()

    lector.revision.return_value = "revision_2"
    lector.listar_hojas.return_value = (replace(primero.hojas[0], ultima_fila_con_cliente=5),)
    lector.leer_clientes.return_value = (
        FilaCliente(fila=3, nombre_crudo="AAA Nueva Persona"),
        FilaCliente(fila=5, nombre_crudo="Prueba, Axa"),
    )
    actualizado = servicio.ejecutar(simulacion=False)
    assert almacen.leer() == actualizado
    axa = next(persona for persona in actualizado.personas if persona.clave_orden == "axa prueba")
    assert axa.persona_id == persona_id
    assert axa.ubicaciones == (UbicacionCliente(hoja="IOL", fila=5),)
    ruta_contactos = tmp_path / "semana.json"
    guardar_json(
        ruta_contactos,
        {
            "rango": {"desde": "2026-11-02", "hasta": "2026-11-06"},
            "contactos": [{"nombre_crudo": "Apodo conocido", "ultima_fecha": "2026-11-05"}],
        },
    )
    coincidencias = ServicioCoincidencias(
        lector_contactos=LectorContactosJson(ruta_contactos),
        almacen_catalogo=almacen,
        almacen_aliases=AlmacenAliasesJson(ruta_aliases),
        lista_exclusion=ListaExclusionJson(tmp_path / "exclusiones.json"),
        almacen_resoluciones=AlmacenResolucionesJson(tmp_path / "informe.json"),
        comparador=ComparadorRapidFuzz(),
    )

    resultado = coincidencias.ejecutar().resueltos[0]

    assert resultado.via is ViaCoincidencia.ALIAS
    assert resultado.persona_id == persona_id
    assert resultado.ubicaciones == (UbicacionCliente(hoja="IOL", fila=5),)
    assert not (tmp_path / "informe.json").exists()


def test_cache_corrupta_solo_se_sustituye_con_forzado_y_ejecucion_real(
    tmp_path: Path, lector: MagicMock
) -> None:
    ruta = tmp_path / "catalogo.json"
    ruta.write_bytes(b"{corrupto")
    almacen = AlmacenCatalogoJson(ruta)
    servicio = ServicioCatalogo(
        lector_clientes=lector,
        almacen_catalogo=almacen,
        reloj=RelojSistema(),
        identificador_origen="origen_sintetico",
    )
    with pytest.raises(EstructuraError):
        servicio.ejecutar(simulacion=False)
    servicio.ejecutar(forzar=True)
    assert ruta.read_bytes() == b"{corrupto"

    reconstruido = servicio.ejecutar(forzar=True, simulacion=False)

    assert almacen.leer() == reconstruido
