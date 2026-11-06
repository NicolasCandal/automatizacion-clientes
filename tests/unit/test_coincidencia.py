"""Decisiones de coincidencia con comparación controlada y sin red ni archivos."""

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from automatizacion_clientes.core.coincidencia import MotorCoincidencias
from automatizacion_clientes.core.errores import ValidacionError
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
    PoliticaCoincidencia,
    ResultadoContacto,
    UbicacionCliente,
    ViaCoincidencia,
)
from automatizacion_clientes.core.normalizacion import clave_orden, normalizar


class ComparadorControlado:
    """Doble estructural que registra consultas y devuelve puntuaciones conocidas."""

    def __init__(self, puntuaciones: Mapping[tuple[str, str], float] | None = None) -> None:
        self.puntuaciones: Mapping[tuple[str, str], float] = puntuaciones or {}
        self.consultas: list[tuple[str, str]] = []

    def similitud(self, nombre_consulta: str, nombre_candidato: str) -> float:
        self.consultas.append((nombre_consulta, nombre_candidato))
        return self.puntuaciones.get((nombre_consulta, nombre_candidato), 0.0)


def crear_persona(persona_id: str, nombre: str, hoja: str = "IOL") -> PersonaCliente:
    return PersonaCliente(
        persona_id=persona_id,
        clave_orden=clave_orden(nombre),
        nombre_canonico=nombre,
        titulares=(nombre,),
        apariciones=(
            AparicionCliente(ubicacion=UbicacionCliente(hoja=hoja, fila=3), nombre_crudo=nombre),
        ),
    )


def crear_catalogo(personas: tuple[PersonaCliente, ...]) -> CatalogoClientes:
    nombres_hojas = sorted({hoja for persona in personas for hoja in persona.hojas})
    return CatalogoClientes(
        identificador_origen="origen_sintetico",
        revision="revision_1",
        generado_en=datetime(2026, 11, 5, tzinfo=UTC),
        hojas=tuple(
            HojaInfo(
                nombre=nombre,
                sheet_id=identificador,
                col_nombre=1,
                primera_col_semana=None,
                ultima_col_semana=None,
                ultima_fila_con_cliente=100,
            )
            for identificador, nombre in enumerate(nombres_hojas)
        ),
        personas=personas,
    )


def resuelto_unico(resultados: tuple[ResultadoContacto, ...]) -> ContactoResuelto:
    assert len(resultados) == 1
    resultado = resultados[0]
    assert isinstance(resultado, ContactoResuelto)
    return resultado


def pendiente_unico(resultados: tuple[ResultadoContacto, ...]) -> ContactoPendiente:
    assert len(resultados) == 1
    resultado = resultados[0]
    assert isinstance(resultado, ContactoPendiente)
    return resultado


@pytest.fixture
def contacto() -> ContactoSemanal:
    return ContactoSemanal(
        nombre_crudo="Axa Prueba", ultima_fecha=date(2026, 11, 5), telefono="+5491100000000"
    )


@pytest.fixture
def persona() -> PersonaCliente:
    persona = crear_persona("p0001", "Axa Radono Prueba")
    return replace(
        persona,
        apariciones=(
            *persona.apariciones,
            AparicionCliente(
                ubicacion=UbicacionCliente(hoja="IOL", fila=10), nombre_crudo="Prueba Axa Radono"
            ),
            AparicionCliente(
                ubicacion=UbicacionCliente(hoja="Bull", fila=5), nombre_crudo="Axa Radono Prueba"
            ),
        ),
    )


@pytest.fixture
def alias(contacto: ContactoSemanal) -> AliasContacto:
    return AliasContacto(
        nombre_crudo=contacto.nombre_crudo,
        telefono=contacto.telefono,
        personas=(DestinoAlias(persona_id="p0001", hojas=frozenset({"IOL"})),),
    )


@pytest.mark.parametrize("sufijo", ["", " (IOL/Bull)", " (cliente habitual)"])
def test_exacto_conserva_todas_las_filas_sin_usar_comparador(
    persona: PersonaCliente, contacto: ContactoSemanal, sufijo: str
) -> None:
    comparador = ComparadorControlado()
    motor = MotorCoincidencias(crear_catalogo((persona,)), comparador)
    resultado = resuelto_unico(
        motor.resolver_contacto(replace(contacto, nombre_crudo=f"PRUEBA, AXA RÁDONO{sufijo}"))
    )

    assert resultado.persona_id == persona.persona_id
    assert resultado.ubicaciones == persona.ubicaciones
    assert resultado.via is ViaCoincidencia.EXACTO
    assert comparador.consultas == []


def test_broker_restringe_hojas_sin_perder_filas_repetidas(
    persona: PersonaCliente, contacto: ContactoSemanal
) -> None:
    motor = MotorCoincidencias(crear_catalogo((persona,)), ComparadorControlado())
    resultado = resuelto_unico(
        motor.resolver_contacto(replace(contacto, nombre_crudo="Axa Radono Prueba (IOL)"))
    )

    assert resultado.ubicaciones == (
        UbicacionCliente(hoja="IOL", fila=3),
        UbicacionCliente(hoja="IOL", fila=10),
    )


def test_exacto_con_broker_incompatible_requiere_revision(
    persona: PersonaCliente, contacto: ContactoSemanal
) -> None:
    motor = MotorCoincidencias(crear_catalogo((persona,)), ComparadorControlado())
    resultado = pendiente_unico(
        motor.resolver_contacto(replace(contacto, nombre_crudo="Axa Radono Prueba (PPI)"))
    )

    assert resultado.motivo is MotivoPendiente.BROKER_NO_COINCIDE
    assert resultado.candidatos[0].puntuacion == 100.0


@pytest.mark.parametrize(
    ("puntuacion", "motivo"),
    [
        (0.0, MotivoPendiente.SIN_MATCH),
        (79.99, MotivoPendiente.SIN_MATCH),
        (80.0, MotivoPendiente.AMBIGUO),
        (91.99, MotivoPendiente.AMBIGUO),
        (92.0, None),
        (100.0, None),
    ],
)
def test_limites_de_los_umbrales(
    persona: PersonaCliente,
    contacto: ContactoSemanal,
    puntuacion: float,
    motivo: MotivoPendiente | None,
) -> None:
    comparador = ComparadorControlado({("axa prueba", "axa radono prueba"): puntuacion})
    motor = MotorCoincidencias(crear_catalogo((persona,)), comparador)
    resultados = motor.resolver_contacto(contacto)

    if motivo is None:
        resultado = resuelto_unico(resultados)
        assert resultado.via is ViaCoincidencia.FUZZY
        assert resultado.puntuacion == puntuacion
    else:
        assert pendiente_unico(resultados).motivo is motivo
    assert comparador.consultas == [("axa prueba", "axa radono prueba")]


@pytest.mark.parametrize("puntuaciones", [(96.0, 94.0), (100.0, 100.0)])
def test_varias_personas_fuertes_son_ambiguas_aunque_el_broker_solo_confirme_una(
    contacto: ContactoSemanal, puntuaciones: tuple[float, float]
) -> None:
    personas = (
        crear_persona("p0001", "Primera Persona"),
        crear_persona("p0002", "Segunda Persona", "Bull"),
    )
    comparador = ComparadorControlado(
        {
            ("axa prueba", "primera persona"): puntuaciones[0],
            ("axa prueba", "segunda persona"): puntuaciones[1],
        }
    )
    motor = MotorCoincidencias(crear_catalogo(personas), comparador)
    resultado = pendiente_unico(
        motor.resolver_contacto(replace(contacto, nombre_crudo="Axa Prueba (IOL)"))
    )

    assert resultado.motivo is MotivoPendiente.AMBIGUO
    assert len(resultado.candidatos) == 2


def test_mejor_candidato_de_otro_broker_no_se_reemplaza_por_uno_mas_debil(
    contacto: ContactoSemanal,
) -> None:
    personas = (
        crear_persona("p0001", "Primera Persona", "Bull"),
        crear_persona("p0002", "Segunda Persona"),
    )
    comparador = ComparadorControlado(
        {
            ("axa prueba", "primera persona"): 99.0,
            ("axa prueba", "segunda persona"): 85.0,
        }
    )
    motor = MotorCoincidencias(crear_catalogo(personas), comparador)

    assert (
        pendiente_unico(
            motor.resolver_contacto(replace(contacto, nombre_crudo="Axa Prueba (IOL)"))
        ).motivo
        is MotivoPendiente.BROKER_NO_COINCIDE
    )


def test_limite_de_sugerencias_no_oculta_competidores_fuertes(contacto: ContactoSemanal) -> None:
    primera = crear_persona("p0001", "Primera Persona")
    primera = replace(primera, titulares=tuple(f"Variante {numero}" for numero in range(12)))
    segunda = crear_persona("p0002", "Segunda Persona")
    puntuaciones = {("axa prueba", normalizar(titular)): 96.0 for titular in primera.titulares}
    puntuaciones[("axa prueba", "segunda persona")] = 94.0
    comparador = ComparadorControlado(puntuaciones)
    motor = MotorCoincidencias(
        crear_catalogo((primera, segunda)),
        comparador,
        politica=PoliticaCoincidencia(limite_sugerencias=1),
    )
    resultado = pendiente_unico(motor.resolver_contacto(contacto))

    assert resultado.motivo is MotivoPendiente.AMBIGUO
    assert len(resultado.candidatos) == 1
    assert len(comparador.consultas) == 13


def test_mejor_variante_de_una_persona_no_crea_ambiguedad(
    persona: PersonaCliente, contacto: ContactoSemanal
) -> None:
    persona = replace(persona, titulares=("Axa Radono Prueba", "Prueba Axa"))
    comparador = ComparadorControlado(
        {
            ("axa prueba", "axa radono prueba"): 95.0,
            ("axa prueba", "prueba axa"): 99.0,
        }
    )
    resultado = resuelto_unico(
        MotorCoincidencias(crear_catalogo((persona,)), comparador).resolver_contacto(contacto)
    )

    assert resultado.puntuacion == 99.0


def test_sugerencias_empatadas_tienen_orden_determinista(contacto: ContactoSemanal) -> None:
    personas = (
        crear_persona("p0002", "Segunda Persona"),
        crear_persona("p0001", "Primera Persona"),
    )
    comparador = ComparadorControlado(
        {
            ("axa prueba", "primera persona"): 85.0,
            ("axa prueba", "segunda persona"): 85.0,
        }
    )
    resultado = pendiente_unico(
        MotorCoincidencias(crear_catalogo(personas), comparador).resolver_contacto(contacto)
    )

    assert tuple(candidato.persona_id for candidato in resultado.candidatos) == ("p0001", "p0002")


@pytest.mark.parametrize("nombre", ["--- (IOL)", "(IOL)"])
def test_nombre_sin_texto_comparable_no_se_resuelve(
    persona: PersonaCliente, contacto: ContactoSemanal, nombre: str
) -> None:
    comparador = ComparadorControlado()
    resultado = pendiente_unico(
        MotorCoincidencias(crear_catalogo((persona,)), comparador).resolver_contacto(
            replace(contacto, nombre_crudo=nombre)
        )
    )

    assert resultado.motivo is MotivoPendiente.SIN_MATCH
    assert resultado.candidatos == ()
    assert comparador.consultas == []


def test_catalogo_vacio_o_titulares_no_comparables_no_generan_candidatos(
    persona: PersonaCliente, contacto: ContactoSemanal
) -> None:
    comparador = ComparadorControlado()
    for personas in ((), (replace(persona, titulares=("---",)),)):
        resultado = pendiente_unico(
            MotorCoincidencias(crear_catalogo(personas), comparador).resolver_contacto(contacto)
        )
        assert resultado.candidatos == ()
    assert comparador.consultas == []


@pytest.mark.parametrize("es_grupo", [True, False])
def test_grupos_y_exclusiones_tienen_prioridad_sin_comparar(
    persona: PersonaCliente, contacto: ContactoSemanal, alias: AliasContacto, es_grupo: bool
) -> None:
    comparador = ComparadorControlado()
    resultados = MotorCoincidencias(crear_catalogo((persona,)), comparador).resolver_contacto(
        replace(contacto, es_grupo=es_grupo), alias_nombre=alias, excluido=True
    )

    assert len(resultados) == 1
    assert isinstance(resultados[0], ContactoIgnorado)
    assert resultados[0].motivo is (
        MotivoIgnorado.GRUPO if es_grupo else MotivoIgnorado.EN_LISTA_NEGRA
    )
    assert comparador.consultas == []


def test_cotitulares_se_resuelven_por_separado_y_no_duplican_personas(
    persona: PersonaCliente, contacto: ContactoSemanal
) -> None:
    otra_persona = crear_persona("p0002", "Daresu Zeta")
    motor = MotorCoincidencias(crear_catalogo((persona, otra_persona)), ComparadorControlado())
    resultados = motor.resolver_contacto(
        replace(contacto, nombre_crudo="Axa Radono Prueba / Daresu Zeta / PRUEBA, AXA RADONO")
    )

    assert len(resultados) == 2
    assert all(isinstance(resultado, ContactoResuelto) for resultado in resultados)


def test_cotitular_pendiente_no_se_convierte_en_resuelto(
    persona: PersonaCliente, contacto: ContactoSemanal
) -> None:
    resultados = MotorCoincidencias(
        crear_catalogo((persona,)), ComparadorControlado()
    ).resolver_contacto(replace(contacto, nombre_crudo="Axa Radono Prueba / Desconocida Persona"))

    assert len(resultados) == 2
    assert isinstance(resultados[0], ContactoResuelto)
    assert isinstance(resultados[1], ContactoPendiente)


def test_alias_nombre_usa_filas_actuales_sin_comparar(
    persona: PersonaCliente, contacto: ContactoSemanal, alias: AliasContacto
) -> None:
    comparador = ComparadorControlado()
    resultado = resuelto_unico(
        MotorCoincidencias(crear_catalogo((persona,)), comparador).resolver_contacto(
            contacto, alias_nombre=alias
        )
    )

    assert resultado.via is ViaCoincidencia.ALIAS
    assert tuple(ubicacion.fila for ubicacion in resultado.ubicaciones) == (3, 10)
    assert comparador.consultas == []


def test_telefono_prevalece_sobre_alias_por_nombre(
    persona: PersonaCliente, contacto: ContactoSemanal, alias: AliasContacto
) -> None:
    otra_persona = crear_persona("p0002", "Daresu Zeta")
    alias_por_nombre = replace(
        alias, personas=(DestinoAlias(persona_id="p0002", hojas=frozenset({"IOL"})),)
    )
    resultado = resuelto_unico(
        MotorCoincidencias(
            crear_catalogo((persona, otra_persona)), ComparadorControlado()
        ).resolver_contacto(
            contacto,
            aliases_telefono=(replace(alias, nombre_crudo="Nombre anterior"),),
            alias_nombre=alias_por_nombre,
        )
    )

    assert resultado.persona_id == "p0001"
    assert resultado.via is ViaCoincidencia.TELEFONO


@pytest.mark.parametrize("telefono", [None, "+5491199999999"])
def test_evidencia_de_telefono_ajeno_se_rechaza(
    persona: PersonaCliente, contacto: ContactoSemanal, alias: AliasContacto, telefono: str | None
) -> None:
    motor = MotorCoincidencias(crear_catalogo((persona,)), ComparadorControlado())

    with pytest.raises(ValidacionError, match="teléfono del contacto"):
        motor.resolver_contacto(replace(contacto, telefono=telefono), aliases_telefono=(alias,))


def test_evidencia_de_nombre_ajeno_se_rechaza(
    persona: PersonaCliente, contacto: ContactoSemanal, alias: AliasContacto
) -> None:
    motor = MotorCoincidencias(crear_catalogo((persona,)), ComparadorControlado())

    with pytest.raises(ValidacionError, match="nombre del contacto"):
        motor.resolver_contacto(contacto, alias_nombre=replace(alias, nombre_crudo="Otro nombre"))


def test_alias_por_nombre_con_telefono_distinto_requiere_revision(
    persona: PersonaCliente, contacto: ContactoSemanal, alias: AliasContacto
) -> None:
    resultado = pendiente_unico(
        MotorCoincidencias(crear_catalogo((persona,)), ComparadorControlado()).resolver_contacto(
            contacto, alias_nombre=replace(alias, telefono="+5491199999999")
        )
    )

    assert resultado.motivo is MotivoPendiente.AMBIGUO


@pytest.mark.parametrize("destinos_coinciden", [True, False])
def test_telefono_compartido_se_resuelve_solo_con_destinos_coherentes(
    persona: PersonaCliente,
    contacto: ContactoSemanal,
    alias: AliasContacto,
    destinos_coinciden: bool,
) -> None:
    otra_persona = crear_persona("p0002", "Daresu Zeta")
    destino_otro = DestinoAlias(persona_id="p0002", hojas=frozenset({"IOL"}))
    primero = replace(alias, personas=(*alias.personas, destino_otro))
    segundo = replace(
        alias,
        nombre_crudo="Otro contacto",
        personas=tuple(reversed(primero.personas)) if destinos_coinciden else (destino_otro,),
    )
    resultados = MotorCoincidencias(
        crear_catalogo((persona, otra_persona)), ComparadorControlado()
    ).resolver_contacto(contacto, aliases_telefono=(primero, segundo))

    if destinos_coinciden:
        assert len(resultados) == 2
        assert all(isinstance(resultado, ContactoResuelto) for resultado in resultados)
    else:
        assert pendiente_unico(resultados).motivo is MotivoPendiente.AMBIGUO


@pytest.mark.parametrize(
    ("persona_id", "hojas", "motivo"),
    [
        ("ausente", frozenset({"IOL"}), MotivoPendiente.SIN_MATCH),
        ("p0001", frozenset({"PPI"}), MotivoPendiente.BROKER_NO_COINCIDE),
    ],
)
def test_alias_obsoleto_va_a_revision_sin_resolucion_parcial(
    persona: PersonaCliente,
    contacto: ContactoSemanal,
    alias: AliasContacto,
    persona_id: str,
    hojas: frozenset[str],
    motivo: MotivoPendiente,
) -> None:
    obsoleto = replace(alias, personas=(DestinoAlias(persona_id=persona_id, hojas=hojas),))
    resultado = pendiente_unico(
        MotorCoincidencias(crear_catalogo((persona,)), ComparadorControlado()).resolver_contacto(
            contacto, alias_nombre=obsoleto
        )
    )

    assert resultado.motivo is motivo


def test_alias_no_ignora_un_broker_explicito_incompatible(
    persona: PersonaCliente, contacto: ContactoSemanal, alias: AliasContacto
) -> None:
    contacto = replace(contacto, nombre_crudo="Axa Prueba (Bull)")
    resultado = pendiente_unico(
        MotorCoincidencias(crear_catalogo((persona,)), ComparadorControlado()).resolver_contacto(
            contacto, aliases_telefono=(alias,)
        )
    )

    assert resultado.motivo is MotivoPendiente.BROKER_NO_COINCIDE


@pytest.mark.parametrize("puntuacion", [float("nan"), float("inf"), -1.0, 101.0, True])
def test_comparador_no_puede_ocultar_puntuacion_invalida_en_una_variante_menor(
    persona: PersonaCliente, contacto: ContactoSemanal, puntuacion: float
) -> None:
    persona = replace(persona, titulares=("Axa Radono Prueba", "Otra variante"))
    comparador = ComparadorControlado(
        {
            ("axa prueba", "axa radono prueba"): 100.0,
            ("axa prueba", "otra variante"): puntuacion,
        }
    )

    with pytest.raises(ValidacionError, match="puntuación"):
        MotorCoincidencias(crear_catalogo((persona,)), comparador).resolver_contacto(contacto)


def test_politica_y_tabla_de_brokers_son_configurables(
    persona: PersonaCliente, contacto: ContactoSemanal
) -> None:
    tabla = {"propio": "IOL"}
    motor = MotorCoincidencias(
        crear_catalogo((persona,)),
        ComparadorControlado({("axa prueba", "axa radono prueba"): 95.0}),
        politica=PoliticaCoincidencia(umbral_automatico=98.0, umbral_revision=90.0),
        tabla_brokers=tabla,
    )
    tabla["propio"] = "PPI"
    resultado = pendiente_unico(
        motor.resolver_contacto(replace(contacto, nombre_crudo="Axa Prueba (propio)"))
    )

    assert resultado.motivo is MotivoPendiente.AMBIGUO


@pytest.mark.parametrize(
    ("automatico", "revision", "limite"),
    [
        (80.0, 80.0, 3),
        (80.0, 92.0, 3),
        (101.0, 80.0, 3),
        (92.0, float("nan"), 3),
        (92.0, 80.0, 0),
        (92.0, 80.0, True),
    ],
)
def test_politica_rechaza_umbrales_o_limites_invalidos(
    automatico: float, revision: float, limite: int
) -> None:
    with pytest.raises(ValidacionError):
        PoliticaCoincidencia(
            umbral_automatico=automatico, umbral_revision=revision, limite_sugerencias=limite
        )
