"""Reconocimiento de brokers y conservación de las reglas de extracción."""

from dataclasses import FrozenInstanceError
from types import MappingProxyType

import pytest

from automatizacion_clientes.core.brokers import extraer_brokers_y_nombre


@pytest.mark.parametrize(
    ("alias", "hoja"),
    [
        ("IOL", "IOL"),
        ("invertironline", "IOL"),
        ("IÓL", "IOL"),
        ("bull", "Bull"),
        ("BM", "Bull"),
        ("BULL MARKET", "Bull"),
        ("Balanz", "Balanz"),
        ("PPI", "PPI"),
        ("Portfolio Personal", "PPI"),
        ("Inviu", "Inviu"),
    ],
)
def test_alias_identifica_una_hoja_canonica(alias: str, hoja: str) -> None:
    resultado = extraer_brokers_y_nombre(f"Contacto de prueba ({alias})")

    assert resultado.nombre_sin_parentesis == "Contacto de prueba"
    assert resultado.hojas == (hoja,)
    assert resultado.ruido == ()


@pytest.mark.parametrize(
    ("contenido", "hojas"),
    [
        ("IOL/Bull", ("IOL", "Bull")),
        ("IOL y Balanz", ("IOL", "Balanz")),
        ("IOL Y PPI", ("IOL", "PPI")),
        ("IOL - PPI", ("IOL", "PPI")),
        ("IOL, Inviu", ("IOL", "Inviu")),
        ("/IOL//Bull/", ("IOL", "Bull")),
        ("Bull/IOL y PPI, Inviu - Balanz", ("Bull", "IOL", "PPI", "Inviu", "Balanz")),
    ],
)
def test_separadores_permiten_varias_hojas_en_orden(contenido: str, hojas: tuple[str, ...]) -> None:
    resultado = extraer_brokers_y_nombre(f"Contacto de prueba ({contenido})")

    assert resultado.hojas == hojas
    assert resultado.ruido == ()


def test_hojas_repetidas_por_alias_o_parentesis_no_se_duplican() -> None:
    resultado = extraer_brokers_y_nombre(
        "Contacto de prueba (Bull/IOL) (BM/invertironline/PPI/Bull Market)"
    )

    assert resultado.nombre_sin_parentesis == "Contacto de prueba"
    assert resultado.hojas == ("Bull", "IOL", "PPI")


def test_ruido_conserva_texto_original_y_repeticiones() -> None:
    resultado = extraer_brokers_y_nombre("Contacto de prueba (IOL - Clíente VIP, Otro/Otro)")

    assert resultado.hojas == ("IOL",)
    assert resultado.ruido == ("Clíente VIP", "Otro", "Otro")


def test_parentesco_no_se_convierte_en_broker() -> None:
    resultado = extraer_brokers_y_nombre("Contacto de prueba (hijo de Otra Persona)")

    assert resultado.hojas == ()
    assert resultado.ruido == ("hijo de Otra Persona",)


@pytest.mark.parametrize(
    ("nombre", "esperado"),
    [
        ("", ""),
        (" \t\n ", ""),
        ("  PRUEBA, Rádono\tMízafe  ", "PRUEBA, Rádono Mízafe"),
        ("Contacto IOL", "Contacto IOL"),
        ("Contacto (IOL", "Contacto (IOL"),
        ("Contacto IOL)", "Contacto IOL)"),
        ("Contacto ()", "Contacto"),
    ],
)
def test_nombre_se_conserva_sin_inferir_brokers_fuera_de_parentesis(
    nombre: str, esperado: str
) -> None:
    resultado = extraer_brokers_y_nombre(nombre)

    assert resultado.nombre_sin_parentesis == esperado
    assert resultado.hojas == ()
    assert resultado.ruido == ()


def test_nombre_sin_texto_puede_conservar_hojas_reconocidas() -> None:
    resultado = extraer_brokers_y_nombre("(IOL)")

    assert resultado.nombre_sin_parentesis == ""
    assert resultado.hojas == ("IOL",)


def test_tabla_personalizada_reemplaza_la_predeterminada_sin_mutarla() -> None:
    tabla = {"especial": "OtraHoja"}
    resultado = extraer_brokers_y_nombre("Contacto (especial/IOL)", tabla)

    assert resultado.hojas == ("OtraHoja",)
    assert resultado.ruido == ("IOL",)
    assert tabla == {"especial": "OtraHoja"}
    assert extraer_brokers_y_nombre("Contacto (especial)").hojas == ()


def test_tabla_vacia_desactiva_el_reconocimiento() -> None:
    resultado = extraer_brokers_y_nombre("Contacto (IOL/Bull)", {})

    assert resultado.hojas == ()
    assert resultado.ruido == ("IOL", "Bull")


def test_tabla_solo_lectura_es_compatible_y_usa_claves_normalizadas() -> None:
    tabla = MappingProxyType({"mi broker": "OtraHoja"})
    resultado = extraer_brokers_y_nombre("Contacto (MÍ BROKER)", tabla)

    assert resultado.hojas == ("OtraHoja",)
    assert resultado.ruido == ()


def test_resultado_es_inmutable_y_no_expone_nombre_o_ruido() -> None:
    resultado = extraer_brokers_y_nombre("Contacto de prueba (IOL, Nota privada)")

    assert "Contacto de prueba" not in repr(resultado)
    assert "Nota privada" not in repr(resultado)
    with pytest.raises(FrozenInstanceError):
        resultado.__setattr__("hojas", ("Bull",))
