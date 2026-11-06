"""Integración del motor puro con RapidFuzz real y un catálogo sintético."""

from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from automatizacion_clientes.core.coincidencia import MotorCoincidencias
from automatizacion_clientes.core.modelos import (
    AparicionCliente,
    CatalogoClientes,
    ContactoPendiente,
    ContactoResuelto,
    ContactoSemanal,
    HojaInfo,
    MotivoPendiente,
    PersonaCliente,
    PoliticaCoincidencia,
    UbicacionCliente,
    ViaCoincidencia,
)
from automatizacion_clientes.core.normalizacion import clave_orden
from automatizacion_clientes.infra.comparador_rapidfuzz import ComparadorRapidFuzz


def crear_catalogo(*nombres: str) -> CatalogoClientes:
    personas = tuple(
        PersonaCliente(
            persona_id=f"p{numero:04d}",
            clave_orden=clave_orden(nombre),
            nombre_canonico=nombre,
            titulares=(nombre,),
            apariciones=(
                AparicionCliente(
                    ubicacion=UbicacionCliente(hoja="IOL", fila=numero + 2),
                    nombre_crudo=nombre,
                ),
            ),
        )
        for numero, nombre in enumerate(nombres, start=1)
    )
    return CatalogoClientes(
        identificador_origen="catalogo_sintetico",
        revision="revision_1",
        generado_en=datetime(2026, 11, 5, tzinfo=UTC),
        hojas=(
            HojaInfo(
                nombre="IOL",
                sheet_id=0,
                col_nombre=1,
                primera_col_semana=None,
                ultima_col_semana=None,
                ultima_fila_con_cliente=len(nombres) + 2,
            ),
        ),
        personas=personas,
    )


def test_segundo_nombre_faltante_se_resuelve_por_comparacion_aproximada() -> None:
    motor = MotorCoincidencias(crear_catalogo("Firuso Baleve Fivora"), ComparadorRapidFuzz())
    contacto = ContactoSemanal(nombre_crudo="FIRUSO FÍVORA (IOL)", ultima_fecha=date(2026, 11, 5))

    resultados = motor.resolver_contacto(contacto)

    assert len(resultados) == 1
    resultado = resultados[0]
    assert isinstance(resultado, ContactoResuelto)
    assert resultado.via is ViaCoincidencia.FUZZY
    assert resultado.puntuacion == 100.0
    assert resultado.ubicaciones == (UbicacionCliente(hoja="IOL", fila=3),)


def test_dos_personas_con_nombre_parcial_compartido_requieren_revision() -> None:
    motor = MotorCoincidencias(
        crear_catalogo("Mizote Sivivu Kukeve", "Mizote Sivivu Gibelo"), ComparadorRapidFuzz()
    )

    resultados = motor.resolver_contacto(
        ContactoSemanal(nombre_crudo="Mizote Sivivu (IOL)", ultima_fecha=date(2026, 11, 5))
    )

    resultado = resultados[0]
    assert isinstance(resultado, ContactoPendiente)
    assert resultado.motivo is MotivoPendiente.AMBIGUO
    assert tuple(candidato.puntuacion for candidato in resultado.candidatos) == (100.0, 100.0)


@pytest.mark.parametrize(
    ("consulta", "candidato", "motivo", "puntuacion_esperada"),
    [
        ("axa", "axxa", MotivoPendiente.AMBIGUO, 85.71428571428571),
        ("axa", "axaxa", MotivoPendiente.SIN_MATCH, 75.0),
        ("zzz", "aaa", MotivoPendiente.SIN_MATCH, 0.0),
    ],
)
def test_puntuaciones_reales_se_clasifican_sin_recortes_previos(
    consulta: str, candidato: str, motivo: MotivoPendiente, puntuacion_esperada: float
) -> None:
    motor = MotorCoincidencias(crear_catalogo(candidato), ComparadorRapidFuzz())

    resultado = motor.resolver_contacto(
        ContactoSemanal(nombre_crudo=consulta, ultima_fecha=date(2026, 11, 5))
    )[0]

    assert isinstance(resultado, ContactoPendiente)
    assert resultado.motivo is motivo
    assert resultado.candidatos[0].puntuacion == pytest.approx(puntuacion_esperada)


def test_broker_incompatible_veta_una_puntuacion_perfecta() -> None:
    motor = MotorCoincidencias(crear_catalogo("Firuso Baleve Fivora"), ComparadorRapidFuzz())

    resultado = motor.resolver_contacto(
        ContactoSemanal(nombre_crudo="Firuso Fivora (Bull)", ultima_fecha=date(2026, 11, 5))
    )[0]

    assert isinstance(resultado, ContactoPendiente)
    assert resultado.motivo is MotivoPendiente.BROKER_NO_COINCIDE
    assert resultado.candidatos[0].puntuacion == 100.0


def test_muchas_variantes_de_una_persona_no_ocultan_a_su_competidor() -> None:
    catalogo = crear_catalogo("Mizote Sivivu Kukeve", "Mizote Sivivu Gibelo")
    primera = replace(
        catalogo.personas[0],
        titulares=tuple(f"Mizote Sivivu Variante {numero}" for numero in range(12)),
    )
    motor = MotorCoincidencias(
        replace(catalogo, personas=(primera, catalogo.personas[1])), ComparadorRapidFuzz()
    )

    resultado = motor.resolver_contacto(
        ContactoSemanal(nombre_crudo="Mizote Sivivu", ultima_fecha=date(2026, 11, 5))
    )[0]

    assert isinstance(resultado, ContactoPendiente)
    assert resultado.motivo is MotivoPendiente.AMBIGUO
    assert len(resultado.candidatos) == 2


def test_politica_configurable_se_aplica_a_la_puntuacion_completa() -> None:
    motor = MotorCoincidencias(
        crear_catalogo("Axxa"),
        ComparadorRapidFuzz(),
        politica=PoliticaCoincidencia(umbral_automatico=84.0, umbral_revision=80.0),
    )

    resultado = motor.resolver_contacto(
        ContactoSemanal(nombre_crudo="Axa", ultima_fecha=date(2026, 11, 5))
    )[0]

    assert isinstance(resultado, ContactoResuelto)
    assert resultado.via is ViaCoincidencia.FUZZY
    assert resultado.puntuacion == pytest.approx(85.71428571428571)
