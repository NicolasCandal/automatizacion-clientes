"""Fuentes JSON validadas frente al catálogo actual del motor."""

from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from automatizacion_clientes.core.coincidencia import MotorCoincidencias
from automatizacion_clientes.core.contratos import AlmacenAliases, LectorContactos, ListaExclusion
from automatizacion_clientes.core.modelos import (
    AliasContacto,
    AparicionCliente,
    CatalogoClientes,
    ContactoIgnorado,
    ContactoPendiente,
    ContactoResuelto,
    ContactoSemanal,
    DestinoAlias,
    HojaInfo,
    MotivoIgnorado,
    MotivoPendiente,
    PersonaCliente,
    ResolucionSemanal,
    UbicacionCliente,
    ViaCoincidencia,
)
from automatizacion_clientes.core.normalizacion import clave_orden
from automatizacion_clientes.infra.almacen_aliases_json import AlmacenAliasesJson
from automatizacion_clientes.infra.almacen_catalogo_json import AlmacenCatalogoJson
from automatizacion_clientes.infra.almacen_resoluciones_json import AlmacenResolucionesJson
from automatizacion_clientes.infra.almacenamiento_json import guardar_json
from automatizacion_clientes.infra.comparador_rapidfuzz import ComparadorRapidFuzz
from automatizacion_clientes.infra.lector_contactos_json import LectorContactosJson
from automatizacion_clientes.infra.lista_exclusion_json import ListaExclusionJson
from automatizacion_clientes.services.coincidencias import ServicioCoincidencias


@pytest.fixture
def catalogo() -> CatalogoClientes:
    personas = tuple(
        PersonaCliente(
            persona_id=identificador,
            clave_orden=clave_orden(nombre),
            nombre_canonico=nombre,
            titulares=(nombre,),
            apariciones=(
                AparicionCliente(
                    ubicacion=UbicacionCliente(hoja="IOL", fila=fila), nombre_crudo=nombre
                ),
            ),
        )
        for identificador, nombre, fila in (
            ("p0001", "Axa Prueba", 30),
            ("p0002", "Notuka Ejemplo", 80),
        )
    )
    catalogo = CatalogoClientes(
        identificador_origen="origen_sintetico",
        revision="revision_actual",
        generado_en=datetime(2026, 11, 5, tzinfo=UTC),
        hojas=(
            HojaInfo(
                nombre="IOL",
                sheet_id=0,
                col_nombre=1,
                primera_col_semana=None,
                ultima_col_semana=None,
                ultima_fila_con_cliente=100,
            ),
        ),
        personas=personas,
    )
    return catalogo


@pytest.fixture
def motor(catalogo: CatalogoClientes) -> MotorCoincidencias:
    return MotorCoincidencias(catalogo, ComparadorRapidFuzz())


def test_alias_recargado_resuelve_nombre_nuevo_en_fila_actual(
    tmp_path: Path, motor: MotorCoincidencias
) -> None:
    ruta = tmp_path / "aliases.json"
    almacen: AlmacenAliases = AlmacenAliasesJson(ruta)
    almacen.registrar(
        AliasContacto(
            nombre_crudo="Apodo anterior",
            telefono="+5491100000000",
            personas=(DestinoAlias(persona_id="p0001", hojas=frozenset({"IOL"})),),
        )
    )
    almacen.guardar()
    almacen = AlmacenAliasesJson(ruta)
    contacto = ContactoSemanal(
        nombre_crudo="Nombre nuevo (IOL)",
        telefono="+5491100000000",
        ultima_fecha=date(2026, 11, 5),
    )

    resultados = motor.resolver_contacto(
        contacto,
        aliases_telefono=almacen.buscar_por_telefono(contacto.telefono or ""),
        alias_nombre=almacen.buscar_por_nombre(contacto.nombre_crudo),
    )

    assert len(resultados) == 1
    resultado = resultados[0]
    assert isinstance(resultado, ContactoResuelto)
    assert resultado.persona_id == "p0001"
    assert resultado.via is ViaCoincidencia.TELEFONO
    assert resultado.ubicaciones == (UbicacionCliente(hoja="IOL", fila=30),)


@pytest.mark.parametrize("destinos_equivalentes", [True, False])
def test_todos_los_aliases_compartidos_llegan_al_motor_para_decidir(
    tmp_path: Path, motor: MotorCoincidencias, destinos_equivalentes: bool
) -> None:
    ruta = tmp_path / "aliases.json"
    almacen: AlmacenAliases = AlmacenAliasesJson(ruta)
    for nombre, persona_id in (
        ("Alias A", "p0001"),
        ("Alias B", "p0001" if destinos_equivalentes else "p0002"),
    ):
        almacen.registrar(
            AliasContacto(
                nombre_crudo=nombre,
                telefono="+5491100000000",
                personas=(DestinoAlias(persona_id=persona_id, hojas=frozenset({"IOL"})),),
            )
        )
    almacen.guardar()
    almacen = AlmacenAliasesJson(ruta)
    contacto = ContactoSemanal(
        nombre_crudo="Otro nombre", telefono="+5491100000000", ultima_fecha=date(2026, 11, 5)
    )

    resultados = motor.resolver_contacto(
        contacto, aliases_telefono=almacen.buscar_por_telefono(contacto.telefono or "")
    )

    assert len(resultados) == 1
    resultado = resultados[0]
    if destinos_equivalentes:
        assert isinstance(resultado, ContactoResuelto)
        assert resultado.persona_id == "p0001"
        assert resultado.via is ViaCoincidencia.TELEFONO
    else:
        assert isinstance(resultado, ContactoPendiente)
        assert resultado.motivo is MotivoPendiente.AMBIGUO
        assert {candidato.persona_id for candidato in resultado.candidatos} == {"p0001", "p0002"}


def test_alias_aprendido_de_persona_ausente_requiere_revision(
    tmp_path: Path, motor: MotorCoincidencias
) -> None:
    ruta = tmp_path / "aliases.json"
    almacen: AlmacenAliases = AlmacenAliasesJson(ruta)
    almacen.registrar(
        AliasContacto(
            nombre_crudo="Apodo conocido",
            personas=(DestinoAlias(persona_id="persona_ausente", hojas=frozenset({"IOL"})),),
        )
    )
    almacen.guardar()
    almacen = AlmacenAliasesJson(ruta)
    contacto = ContactoSemanal(nombre_crudo="Apodo conocido", ultima_fecha=date(2026, 11, 5))

    resultados = motor.resolver_contacto(
        contacto, alias_nombre=almacen.buscar_por_nombre(contacto.nombre_crudo)
    )

    assert len(resultados) == 1
    resultado = resultados[0]
    assert isinstance(resultado, ContactoPendiente)
    assert resultado.motivo is MotivoPendiente.SIN_MATCH


def test_exclusion_recargada_tiene_prioridad_sobre_alias_por_telefono(
    tmp_path: Path, motor: MotorCoincidencias
) -> None:
    ruta_aliases = tmp_path / "aliases.json"
    almacen: AlmacenAliases = AlmacenAliasesJson(ruta_aliases)
    almacen.registrar(
        AliasContacto(
            nombre_crudo="Apodo anterior",
            telefono="+5491100000000",
            personas=(DestinoAlias(persona_id="p0001", hojas=frozenset({"IOL"})),),
        )
    )
    almacen.guardar()
    almacen = AlmacenAliasesJson(ruta_aliases)
    ruta_exclusiones = tmp_path / "exclusiones.json"
    lista: ListaExclusion = ListaExclusionJson(ruta_exclusiones)
    lista.agregar("Llamada del Contador (IOL)")
    lista = ListaExclusionJson(ruta_exclusiones)
    contacto = ContactoSemanal(
        nombre_crudo="  LLAMADA DEL CONTÁDOR (IOL)  ",
        telefono="+5491100000000",
        ultima_fecha=date(2026, 11, 5),
    )
    aliases = almacen.buscar_por_telefono(contacto.telefono or "")

    sin_exclusion = motor.resolver_contacto(contacto, aliases_telefono=aliases)
    con_exclusion = motor.resolver_contacto(
        contacto, aliases_telefono=aliases, excluido=lista.contiene(contacto.nombre_crudo)
    )

    assert len(sin_exclusion) == len(con_exclusion) == 1
    assert isinstance(sin_exclusion[0], ContactoResuelto)
    resultado = con_exclusion[0]
    assert isinstance(resultado, ContactoIgnorado)
    assert resultado.motivo is MotivoIgnorado.EN_LISTA_NEGRA


def test_entrada_semanal_json_se_resuelve_y_persiste_sin_perder_grupos_ni_fechas(
    tmp_path: Path, motor: MotorCoincidencias
) -> None:
    ruta = tmp_path / "semana.json"
    guardar_json(
        ruta,
        {
            "rango": {"desde": "2026-11-02", "hasta": "2026-11-06"},
            "extraido_en": "2026-11-06T18:00:00-03:00",
            "total_chats_escaneados": 3,
            "contactos": [
                {"nombre_crudo": "Axa Prueba (IOL)", "ultima_fecha": "2026-11-02"},
                {
                    "nombre_crudo": "Grupo de prueba",
                    "ultima_fecha": "2026-11-06",
                    "es_grupo": True,
                },
                {"nombre_crudo": "ZZZZZZZZZ", "ultima_fecha": "2026-11-05"},
            ],
        },
    )
    lector: LectorContactos = LectorContactosJson(ruta)

    semana = lector.leer()
    resultados = tuple(motor.resolver_contacto(contacto)[0] for contacto in semana.contactos)

    resuelto, ignorado, pendiente = resultados
    assert isinstance(resuelto, ContactoResuelto)
    assert resuelto.via is ViaCoincidencia.EXACTO
    assert resuelto.ubicaciones == (UbicacionCliente(hoja="IOL", fila=30),)
    assert resuelto.contacto.ultima_fecha == date(2026, 11, 2)
    assert isinstance(ignorado, ContactoIgnorado)
    assert ignorado.motivo is MotivoIgnorado.GRUPO
    assert ignorado.contacto.ultima_fecha == date(2026, 11, 6)
    assert isinstance(pendiente, ContactoPendiente)
    assert pendiente.motivo is MotivoPendiente.SIN_MATCH
    informe = ResolucionSemanal(
        resueltos=tuple(
            resultado for resultado in resultados if isinstance(resultado, ContactoResuelto)
        ),
        pendientes=tuple(
            resultado for resultado in resultados if isinstance(resultado, ContactoPendiente)
        ),
        ignorados=tuple(
            resultado for resultado in resultados if isinstance(resultado, ContactoIgnorado)
        ),
        avisos=("Informe de integración",),
    )
    almacen = AlmacenResolucionesJson(tmp_path / "salida" / "informe.json")
    almacen.guardar(informe)

    assert almacen.leer() == informe


def test_catalogo_recargado_entrega_todas_las_filas_al_motor(
    tmp_path: Path, catalogo: CatalogoClientes
) -> None:
    persona = catalogo.personas[0]
    persona = replace(
        persona,
        apariciones=(
            *persona.apariciones,
            AparicionCliente(
                ubicacion=UbicacionCliente(hoja="IOL", fila=79), nombre_crudo="Prueba, Axa"
            ),
        ),
    )
    catalogo = replace(catalogo, personas=(persona, *catalogo.personas[1:]))
    almacen = AlmacenCatalogoJson(tmp_path / "catalogo.json")
    almacen.guardar(catalogo)
    recargado = almacen.leer()
    assert recargado is not None
    motor = MotorCoincidencias(recargado, ComparadorRapidFuzz())

    resultados = motor.resolver_contacto(
        ContactoSemanal(nombre_crudo="Axa Prueba (IOL)", ultima_fecha=date(2026, 11, 5))
    )

    assert len(resultados) == 1
    resultado = resultados[0]
    assert isinstance(resultado, ContactoResuelto)
    assert resultado.persona_id == persona.persona_id
    assert resultado.via is ViaCoincidencia.EXACTO
    assert resultado.ubicaciones == (
        UbicacionCliente(hoja="IOL", fila=30),
        UbicacionCliente(hoja="IOL", fila=79),
    )


def test_servicio_simula_sin_archivos_y_real_conecta_todos_los_adaptadores(
    tmp_path: Path, catalogo: CatalogoClientes
) -> None:
    ruta_contactos = tmp_path / "semana.json"
    ruta_catalogo = tmp_path / "catalogo.json"
    ruta_aliases = tmp_path / "estado" / "aliases.json"
    ruta_exclusiones = tmp_path / "estado" / "exclusiones.json"
    ruta_informe = tmp_path / "salida" / "informe.json"
    guardar_json(
        ruta_contactos,
        {
            "rango": {"desde": "2026-11-02", "hasta": "2026-11-06"},
            "contactos": [
                {"nombre_crudo": "Axa Prueba (IOL)", "ultima_fecha": "2026-11-05"},
                {"nombre_crudo": "Grupo", "ultima_fecha": "2026-11-05", "es_grupo": True},
                {"nombre_crudo": "ZZZZZZZ", "ultima_fecha": "2026-11-05"},
            ],
        },
    )
    catalogos = AlmacenCatalogoJson(ruta_catalogo)
    catalogos.guardar(catalogo)
    originales = (ruta_contactos.read_bytes(), ruta_catalogo.read_bytes())
    aliases = AlmacenAliasesJson(ruta_aliases)
    informes = AlmacenResolucionesJson(ruta_informe)
    servicio = ServicioCoincidencias(
        lector_contactos=LectorContactosJson(ruta_contactos),
        almacen_catalogo=catalogos,
        almacen_aliases=aliases,
        lista_exclusion=ListaExclusionJson(ruta_exclusiones),
        almacen_resoluciones=informes,
        comparador=ComparadorRapidFuzz(),
    )

    simulado = servicio.ejecutar()

    assert len(simulado.resueltos) == len(simulado.pendientes) == len(simulado.ignorados) == 1
    assert aliases.buscar_por_nombre("Axa Prueba (IOL)") is None
    assert not ruta_aliases.parent.exists()
    assert not ruta_informe.parent.exists()
    real = servicio.ejecutar(simulacion=False)

    assert real == simulado
    assert informes.leer() == real
    aprendido = AlmacenAliasesJson(ruta_aliases).buscar_por_nombre("Axa Prueba (IOL)")
    assert aprendido is not None
    assert aprendido.personas == (DestinoAlias(persona_id="p0001", hojas=frozenset({"IOL"})),)
    assert not ruta_exclusiones.exists()
    assert (ruta_contactos.read_bytes(), ruta_catalogo.read_bytes()) == originales
