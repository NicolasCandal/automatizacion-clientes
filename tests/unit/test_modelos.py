"""Invariantes del dominio con ejemplos sintéticos y sin acceso al exterior."""

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime

import pytest

from automatizacion_clientes.core.errores import ValidacionError
from automatizacion_clientes.core.modelos import (
    AparicionCliente,
    CandidatoCoincidencia,
    CatalogoClientes,
    ContactoIgnorado,
    ContactoPendiente,
    ContactoResuelto,
    ContactoSemanal,
    FilaCliente,
    HojaInfo,
    MotivoIgnorado,
    MotivoPendiente,
    PeriodoSemanal,
    PersonaCliente,
    ResolucionSemanal,
    ResultadoContacto,
    SemanaContactos,
    UbicacionCliente,
    ViaCoincidencia,
)


@pytest.fixture
def periodo() -> PeriodoSemanal:
    return PeriodoSemanal(desde=date(2026, 11, 2), hasta=date(2026, 11, 6))


@pytest.fixture
def contacto() -> ContactoSemanal:
    return ContactoSemanal(
        nombre_crudo="Contacto de prueba (IOL)",
        ultima_fecha=date(2026, 11, 5),
        telefono="+5491100000000",
    )


@pytest.fixture
def ubicacion() -> UbicacionCliente:
    return UbicacionCliente(hoja="IOL", fila=3)


@pytest.fixture
def persona(ubicacion: UbicacionCliente) -> PersonaCliente:
    return PersonaCliente(
        persona_id="p0001",
        clave_orden="cliente prueba",
        nombre_canonico="Cliente Prueba",
        titulares=("Cliente Prueba",),
        apariciones=(AparicionCliente(ubicacion=ubicacion, nombre_crudo="Cliente Prueba"),),
    )


@pytest.fixture
def hoja() -> HojaInfo:
    return HojaInfo(
        nombre="IOL",
        sheet_id=0,
        col_nombre=1,
        primera_col_semana=3,
        ultima_col_semana=10,
        ultima_fila_con_cliente=30,
    )


@pytest.fixture
def catalogo(hoja: HojaInfo, persona: PersonaCliente) -> CatalogoClientes:
    return CatalogoClientes(
        identificador_origen="origen_de_prueba",
        revision="revision_1",
        generado_en=datetime(2026, 11, 5, 12, tzinfo=UTC),
        hojas=(hoja,),
        personas=(persona,),
    )


@pytest.fixture
def candidato(persona: PersonaCliente) -> CandidatoCoincidencia:
    return CandidatoCoincidencia(
        persona_id=persona.persona_id,
        nombre_canonico=persona.nombre_canonico,
        hojas=persona.hojas,
        puntuacion=95.5,
        via=ViaCoincidencia.FUZZY,
    )


def test_periodo_incluye_extremos_y_excluye_dias_adyacentes(periodo: PeriodoSemanal) -> None:
    assert periodo.contiene(date(2026, 11, 2))
    assert periodo.contiene(date(2026, 11, 6))
    assert not periodo.contiene(date(2026, 11, 1))
    assert not periodo.contiene(date(2026, 11, 7))


def test_periodo_admite_un_solo_dia_y_cambio_de_anio() -> None:
    mismo_dia = PeriodoSemanal(desde=date(2026, 11, 5), hasta=date(2026, 11, 5))
    cambio_anio = PeriodoSemanal(desde=date(2026, 12, 28), hasta=date(2027, 1, 1))

    assert mismo_dia.contiene(date(2026, 11, 5))
    assert cambio_anio.contiene(date(2027, 1, 1))


def test_periodo_rechaza_rango_invertido() -> None:
    with pytest.raises(ValidacionError, match="fecha final"):
        PeriodoSemanal(desde=date(2026, 11, 6), hasta=date(2026, 11, 2))


def test_periodo_no_acepta_instantes_como_fechas(periodo: PeriodoSemanal) -> None:
    instante = datetime(2026, 11, 5, tzinfo=UTC)

    with pytest.raises(ValidacionError, match="sin hora"):
        replace(periodo, desde=instante)
    with pytest.raises(ValidacionError, match="sin hora"):
        replace(periodo, hasta=instante)
    with pytest.raises(ValidacionError, match="incluir hora"):
        periodo.contiene(instante)


def test_hoja_admite_id_cero_y_ausencia_de_semanas(hoja: HojaInfo) -> None:
    vacia = replace(
        hoja, primera_col_semana=None, ultima_col_semana=None, ultima_fila_con_cliente=2
    )

    assert vacia.sheet_id == 0
    assert vacia.primera_col_semana is None
    assert vacia.ultima_fila_con_cliente == 2


@pytest.mark.parametrize(
    ("primera", "ultima"),
    [(None, 5), (3, None), (0, 5), (3, 0), (1, 5), (5, 3)],
)
def test_hoja_rechaza_limites_semanales_inconsistentes(
    hoja: HojaInfo, primera: int | None, ultima: int | None
) -> None:
    with pytest.raises(ValidacionError):
        replace(hoja, primera_col_semana=primera, ultima_col_semana=ultima)


@pytest.mark.parametrize("fila", [0, -1, True])
def test_referencias_rechazan_filas_invalidas(fila: int) -> None:
    with pytest.raises(ValidacionError, match="entero"):
        FilaCliente(fila=fila, nombre_crudo="Cliente Prueba")
    with pytest.raises(ValidacionError, match="entero"):
        UbicacionCliente(hoja="IOL", fila=fila)


def test_hoja_rechaza_indices_invalidos(hoja: HojaInfo) -> None:
    with pytest.raises(ValidacionError, match="entero"):
        replace(hoja, sheet_id=-1)
    with pytest.raises(ValidacionError, match="entero"):
        replace(hoja, col_nombre=0)
    with pytest.raises(ValidacionError, match="entero"):
        replace(hoja, ultima_fila_con_cliente=-1)


@pytest.mark.parametrize("ultima_fila", [0, 2])
def test_catalogo_rechaza_ubicaciones_posteriores_a_ultima_fila(
    catalogo: CatalogoClientes, hoja: HojaInfo, ultima_fila: int
) -> None:
    with pytest.raises(ValidacionError, match="última fila"):
        replace(catalogo, hojas=(replace(hoja, ultima_fila_con_cliente=ultima_fila),))


def test_catalogo_admite_ubicacion_exactamente_en_ultima_fila(
    catalogo: CatalogoClientes, hoja: HojaInfo
) -> None:
    ajustado = replace(catalogo, hojas=(replace(hoja, ultima_fila_con_cliente=3),))

    assert ajustado.personas[0].ubicaciones == (UbicacionCliente(hoja="IOL", fila=3),)


@pytest.mark.parametrize("nombre", ["", "  ", "\n\t"])
def test_modelos_rechazan_textos_vacios(nombre: str, ubicacion: UbicacionCliente) -> None:
    with pytest.raises(ValidacionError, match="vacío"):
        FilaCliente(fila=3, nombre_crudo=nombre)
    with pytest.raises(ValidacionError, match="vacío"):
        UbicacionCliente(hoja=nombre, fila=3)
    with pytest.raises(ValidacionError, match="vacío"):
        AparicionCliente(ubicacion=ubicacion, nombre_crudo=nombre)


def test_nombre_original_no_se_normaliza_ni_se_expone_en_repr() -> None:
    nombre_original = "  PRUEBA, Cliente Y/O Otra Persona  "
    fila = FilaCliente(fila=3, nombre_crudo=nombre_original)

    assert fila.nombre_crudo == nombre_original
    assert nombre_original not in repr(fila)


def test_persona_conserva_varias_filas_de_una_hoja_y_varios_brokers(
    persona: PersonaCliente,
) -> None:
    segunda_fila = AparicionCliente(
        ubicacion=UbicacionCliente(hoja="IOL", fila=20), nombre_crudo="Prueba, Cliente"
    )
    otro_broker = AparicionCliente(
        ubicacion=UbicacionCliente(hoja="Bull", fila=9), nombre_crudo="Cliente Prueba"
    )
    persona_completa = replace(
        persona, apariciones=(*persona.apariciones, segunda_fila, otro_broker)
    )

    assert persona_completa.hojas == frozenset({"IOL", "Bull"})
    assert persona_completa.ubicaciones == (
        UbicacionCliente(hoja="IOL", fila=3),
        UbicacionCliente(hoja="IOL", fila=20),
        UbicacionCliente(hoja="Bull", fila=9),
    )


def test_catalogo_admite_cotitulares_en_una_misma_fila(
    catalogo: CatalogoClientes, persona: PersonaCliente
) -> None:
    cotitular = replace(
        persona,
        persona_id="p0002",
        clave_orden="otra persona",
        nombre_canonico="Otra Persona",
        titulares=("Otra Persona",),
    )
    conjunto = replace(catalogo, personas=(persona, cotitular))

    assert conjunto.personas[0].ubicaciones == conjunto.personas[1].ubicaciones


def test_persona_requiere_titulares_y_apariciones(persona: PersonaCliente) -> None:
    with pytest.raises(ValidacionError, match="titulares y apariciones"):
        replace(persona, titulares=())
    with pytest.raises(ValidacionError, match="titulares y apariciones"):
        replace(persona, apariciones=())


def test_persona_rechaza_titulares_y_ubicaciones_duplicadas(persona: PersonaCliente) -> None:
    with pytest.raises(ValidacionError, match="duplicados"):
        replace(persona, titulares=("Cliente Prueba", "Cliente Prueba"))
    with pytest.raises(ValidacionError, match="duplicados"):
        replace(persona, apariciones=persona.apariciones * 2)


def test_catalogo_requiere_fecha_con_zona_horaria(catalogo: CatalogoClientes) -> None:
    with pytest.raises(ValidacionError, match="zona horaria"):
        replace(catalogo, generado_en=datetime(2026, 11, 5))


def test_catalogo_rechaza_hojas_repetidas(catalogo: CatalogoClientes, hoja: HojaInfo) -> None:
    with pytest.raises(ValidacionError, match="nombres de hoja"):
        replace(catalogo, hojas=(hoja, replace(hoja, sheet_id=1)))
    with pytest.raises(ValidacionError, match="identificadores de hoja"):
        replace(catalogo, hojas=(hoja, replace(hoja, nombre="Bull")))


def test_catalogo_rechaza_identidades_y_claves_duplicadas(
    catalogo: CatalogoClientes, persona: PersonaCliente
) -> None:
    with pytest.raises(ValidacionError, match="Las personas"):
        replace(catalogo, personas=(persona, persona))
    with pytest.raises(ValidacionError, match="claves de orden"):
        replace(catalogo, personas=(persona, replace(persona, persona_id="p0002")))


def test_catalogo_rechaza_referencias_a_hojas_ausentes(catalogo: CatalogoClientes) -> None:
    with pytest.raises(ValidacionError, match="hoja ausente"):
        replace(catalogo, hojas=())


def test_catalogo_admite_una_planilla_sin_personas(catalogo: CatalogoClientes) -> None:
    vacio = replace(catalogo, personas=())

    assert vacio.personas == ()


def test_contacto_admite_solo_telefono_y_oculta_identidad_en_repr(
    contacto: ContactoSemanal,
) -> None:
    sin_nombre = replace(contacto, nombre_crudo="")

    assert sin_nombre.telefono == contacto.telefono
    assert contacto.nombre_crudo not in repr(contacto)
    assert contacto.telefono is not None
    assert contacto.telefono not in repr(contacto)


def test_contacto_rechaza_identidad_ausente_o_telefono_vacio(contacto: ContactoSemanal) -> None:
    with pytest.raises(ValidacionError, match="nombre o teléfono"):
        replace(contacto, nombre_crudo=" ", telefono=None)
    with pytest.raises(ValidacionError, match="teléfono"):
        replace(contacto, telefono=" ")


def test_contacto_admite_nombre_sin_telefono(contacto: ContactoSemanal) -> None:
    assert replace(contacto, telefono=None).nombre_crudo == contacto.nombre_crudo


def test_contacto_rechaza_instantes_sin_convertir_zona_horaria(
    contacto: ContactoSemanal,
) -> None:
    with pytest.raises(ValidacionError, match="incluir hora"):
        replace(contacto, ultima_fecha=datetime(2026, 11, 5, tzinfo=UTC))


def test_semana_admite_cero_contactos_o_contactos_dentro_del_periodo(
    periodo: PeriodoSemanal, contacto: ContactoSemanal
) -> None:
    assert SemanaContactos(periodo=periodo, contactos=()).contactos == ()
    assert SemanaContactos(periodo=periodo, contactos=(contacto,)).contactos == (contacto,)


def test_semana_rechaza_contactos_fuera_del_periodo(
    periodo: PeriodoSemanal, contacto: ContactoSemanal
) -> None:
    con_fecha_ajena = replace(contacto, ultima_fecha=date(2026, 11, 1))

    with pytest.raises(ValidacionError, match="período informado"):
        SemanaContactos(periodo=periodo, contactos=(contacto, con_fecha_ajena))


@pytest.mark.parametrize(
    "puntuacion", [-0.1, 100.1, float("nan"), float("inf"), -float("inf"), True]
)
def test_candidato_y_resolucion_rechazan_puntuaciones_invalidas(
    candidato: CandidatoCoincidencia,
    contacto: ContactoSemanal,
    ubicacion: UbicacionCliente,
    puntuacion: float,
) -> None:
    with pytest.raises(ValidacionError, match="puntuación"):
        replace(candidato, puntuacion=puntuacion)
    with pytest.raises(ValidacionError, match="puntuación"):
        ContactoResuelto(
            contacto=contacto,
            persona_id="p0001",
            ubicaciones=(ubicacion,),
            puntuacion=puntuacion,
            via=ViaCoincidencia.FUZZY,
        )


@pytest.mark.parametrize("puntuacion", [0.0, 80.0, 92.0, 100.0])
def test_modelos_no_deciden_umbrales_de_coincidencia(
    candidato: CandidatoCoincidencia, puntuacion: float
) -> None:
    assert replace(candidato, puntuacion=puntuacion).puntuacion == puntuacion


def test_candidato_requiere_hojas_con_nombre(candidato: CandidatoCoincidencia) -> None:
    with pytest.raises(ValidacionError, match="alguna hoja"):
        replace(candidato, hojas=frozenset())
    with pytest.raises(ValidacionError, match="vacío"):
        replace(candidato, hojas=frozenset({" "}))


def test_resuelto_conserva_todas_las_filas_y_rechaza_destinos_invalidos(
    contacto: ContactoSemanal, persona: PersonaCliente, ubicacion: UbicacionCliente
) -> None:
    resultado = ContactoResuelto(
        contacto=contacto,
        persona_id=persona.persona_id,
        ubicaciones=(ubicacion, replace(ubicacion, fila=20)),
        puntuacion=100.0,
        via=ViaCoincidencia.EXACTO,
    )

    assert len(resultado.ubicaciones) == 2
    with pytest.raises(ValidacionError, match="alguna ubicación"):
        replace(resultado, ubicaciones=())
    with pytest.raises(ValidacionError, match="duplicados"):
        replace(resultado, ubicaciones=(ubicacion, ubicacion))


def test_pendiente_admite_ausencia_de_candidatos_y_rechaza_duplicados(
    contacto: ContactoSemanal, candidato: CandidatoCoincidencia
) -> None:
    pendiente = ContactoPendiente(contacto=contacto, motivo=MotivoPendiente.SIN_MATCH)

    assert pendiente.candidatos == ()
    with pytest.raises(ValidacionError, match="duplicados"):
        replace(pendiente, candidatos=(candidato, replace(candidato, puntuacion=90.0)))


def test_resultados_tienen_estados_distintos_y_motivos_tipados(
    contacto: ContactoSemanal, ubicacion: UbicacionCliente
) -> None:
    resuelto = ContactoResuelto(
        contacto=contacto,
        persona_id="p0001",
        ubicaciones=(ubicacion,),
        puntuacion=100.0,
        via=ViaCoincidencia.ALIAS,
    )
    pendiente = ContactoPendiente(contacto=contacto, motivo=MotivoPendiente.AMBIGUO)
    ignorado = ContactoIgnorado(contacto=contacto, motivo=MotivoIgnorado.EN_LISTA_NEGRA)
    resultados: tuple[ResultadoContacto, ...] = (resuelto, pendiente, ignorado)

    assert tuple(resultado.tipo for resultado in resultados) == (
        "resuelto",
        "pendiente",
        "ignorado",
    )
    assert not hasattr(pendiente, "ubicaciones")
    assert not hasattr(ignorado, "persona_id")


def test_resolucion_admite_cotitulares_de_un_mismo_contacto(
    contacto: ContactoSemanal, ubicacion: UbicacionCliente
) -> None:
    titular = ContactoResuelto(
        contacto=contacto,
        persona_id="p0001",
        ubicaciones=(ubicacion,),
        puntuacion=100.0,
        via=ViaCoincidencia.TELEFONO,
    )
    cotitular = replace(titular, persona_id="p0002")
    resolucion = ResolucionSemanal(resueltos=(titular, cotitular), avisos=("Aviso de prueba",))

    assert resolucion.resueltos[0].contacto == resolucion.resueltos[1].contacto
    assert resolucion.resueltos[0].persona_id != resolucion.resueltos[1].persona_id
    assert resolucion.avisos == ("Aviso de prueba",)
    assert ResolucionSemanal().resueltos == ()


def test_modelos_impiden_modificar_la_identidad(persona: PersonaCliente) -> None:
    with pytest.raises(FrozenInstanceError):
        persona.__setattr__("persona_id", "otra_identidad")


def test_codigos_de_negocio_conservan_los_valores_actuales() -> None:
    assert {via.value for via in ViaCoincidencia} == {"telefono", "alias", "exacto", "fuzzy"}
    assert {motivo.value for motivo in MotivoPendiente} == {
        "ambiguo",
        "sin_match",
        "broker_no_coincide",
    }
    assert {motivo.value for motivo in MotivoIgnorado} == {"grupo", "en_lista_negra"}
