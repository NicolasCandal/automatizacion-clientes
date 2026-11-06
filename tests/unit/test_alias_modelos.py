"""Invariantes de las identidades persistidas para resolver contactos futuros."""

from dataclasses import replace

import pytest

from automatizacion_clientes.core.errores import ValidacionError
from automatizacion_clientes.core.modelos import AliasContacto, DestinoAlias


@pytest.fixture
def destino() -> DestinoAlias:
    return DestinoAlias(persona_id="p0001", hojas=frozenset({"IOL", "Bull"}))


def test_alias_conserva_cotitulares_y_hojas_sin_persistir_filas(destino: DestinoAlias) -> None:
    cotitular = replace(destino, persona_id="p0002")
    alias = AliasContacto(nombre_crudo="Contacto de prueba", personas=(destino, cotitular))

    assert len(alias.personas) == 2
    assert alias.personas[0].hojas == frozenset({"IOL", "Bull"})
    assert not hasattr(alias.personas[0], "fila")
    assert alias.telefono is None


def test_alias_admite_telefono_sin_nombre_y_no_expone_identidad(destino: DestinoAlias) -> None:
    alias = AliasContacto(nombre_crudo="", telefono="+5491100000000", personas=(destino,))

    assert alias.nombre_crudo == ""
    assert alias.telefono == "+5491100000000"
    assert "+5491100000000" not in repr(alias)
    assert "p0001" not in repr(alias)


def test_alias_conserva_el_nombre_original(destino: DestinoAlias) -> None:
    nombre = "  PRUEBA, Contacto (IOL)  "
    alias = AliasContacto(nombre_crudo=nombre, personas=(destino,))

    assert alias.nombre_crudo == nombre
    assert nombre not in repr(alias)


def test_alias_rechaza_contacto_sin_identidad(destino: DestinoAlias) -> None:
    with pytest.raises(ValidacionError, match="nombre o teléfono"):
        AliasContacto(nombre_crudo=" ", personas=(destino,))
    with pytest.raises(ValidacionError, match="teléfono"):
        AliasContacto(nombre_crudo="Contacto de prueba", telefono=" ", personas=(destino,))


def test_alias_requiere_destinos_sin_personas_repetidas(destino: DestinoAlias) -> None:
    with pytest.raises(ValidacionError, match="alguna persona"):
        AliasContacto(nombre_crudo="Contacto de prueba", personas=())
    with pytest.raises(ValidacionError, match="duplicados"):
        AliasContacto(
            nombre_crudo="Contacto de prueba",
            personas=(destino, replace(destino, hojas=frozenset({"Balanz"}))),
        )


def test_destino_requiere_persona_y_hojas_con_nombre(destino: DestinoAlias) -> None:
    with pytest.raises(ValidacionError, match="identificador"):
        replace(destino, persona_id=" ")
    with pytest.raises(ValidacionError, match="alguna hoja"):
        replace(destino, hojas=frozenset())
    with pytest.raises(ValidacionError, match="hoja del alias"):
        replace(destino, hojas=frozenset({" "}))
