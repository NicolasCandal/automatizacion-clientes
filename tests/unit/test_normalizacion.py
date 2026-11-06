"""Casos de nombres y reglas históricas de normalización sin dependencias externas."""

from dataclasses import FrozenInstanceError

import pytest

from automatizacion_clientes.core.normalizacion import (
    clave_orden,
    extraer_titulares,
    generar_claves,
    normalizar,
    quitar_tildes,
)


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("", ""),
        (" \n\t ", ""),
        ("--- / () !!!", ""),
        ("Fédezu, Lídexe Bekaxo", "fedezu lidexe bekaxo"),
        ("  PEXEZA   LEVOBA  ", "pexeza levoba"),
        ("Sábili", "sabili"),
        ("Nin\u0303o", "nino"),
        ("Rádono\t\xa0Prueba\nZeta", "radono prueba zeta"),
        ("Texto-Uno O'Filtro", "texto uno o filtro"),
        ("D'Ábaco & Prueba-Zeta", "d abaco prueba zeta"),
        ("CLIENTE_123", "cliente_123"),
        ("Mízafe (IOL)", "mizafe iol"),
        ("Straße", "straße"),
        ("\uff23\uff2c\uff29\uff25\uff2e\uff34\uff25", "cliente"),
        ("ﬂujo", "flujo"),
        ("\u0301", ""),
    ],
)
def test_normalizar_conserva_las_reglas_de_comparacion(texto: str, esperado: str) -> None:
    assert normalizar(texto) == esperado


def test_quitar_tildes_conserva_mayusculas_y_espacios() -> None:
    assert quitar_tildes("  ÁÉÍÓÚ Ñ ﬂ  ") == "  AEIOU N fl  "
    assert quitar_tildes("") == ""


@pytest.mark.parametrize(
    ("nombre", "variante"),
    [
        ("Zokasu, Sisopa", "ZOKASU, SISOPA"),
        ("Vurule, Lasise Guremo", "Lasise Guremo Vurule"),
        ("Gogote Raxozi", "RAXOZI GOGOTE"),
        ("Prueba, Rádono Mízafe", "Mizafe Radono Prueba"),
    ],
)
def test_clave_orden_identifica_variantes_de_nombre(nombre: str, variante: str) -> None:
    assert clave_orden(nombre) == clave_orden(variante)


def test_clave_orden_no_descarta_palabras_repetidas() -> None:
    assert clave_orden("Axa Axa Prueba") == "axa axa prueba"
    assert clave_orden("Axa Axa Prueba") != clave_orden("Axa Prueba")
    assert clave_orden("") == ""


def test_generar_claves_conserva_original_y_devuelve_un_modelo_inmutable() -> None:
    original = "  PRUEBA, Rádono Mízafe  "
    claves = generar_claves(original)

    assert claves.crudo == original
    assert claves.normalizado == "prueba radono mizafe"
    assert claves.clave_orden == "mizafe prueba radono"
    assert original not in repr(claves)
    assert claves.normalizado not in repr(claves)
    assert claves.clave_orden not in repr(claves)
    with pytest.raises(FrozenInstanceError):
        claves.__setattr__("crudo", "Otro nombre")


def test_generar_claves_admite_entrada_sin_nombre() -> None:
    claves = generar_claves("  ")

    assert claves.crudo == "  "
    assert claves.normalizado == ""
    assert claves.clave_orden == ""


def test_orden_de_operaciones_unicode_no_cambia_claves_historicas() -> None:
    # NFKD convierte esta letra modificadora en una A mayúscula. Se conserva
    # la segunda normalización histórica al generar la clave ordenada.
    claves = generar_claves("\u1d2c")

    assert claves.normalizado == "A"
    assert claves.clave_orden == "a"


@pytest.mark.parametrize(
    ("nombre", "titulares"),
    [
        ("", ()),
        (" \n ", ()),
        ("Fedezu, Lidexe Bekaxo", ("Fedezu, Lidexe Bekaxo",)),
        ("  Prueba, Rádono  ", ("Prueba, Rádono",)),
        (
            "RAXOZI GOGOTE Y/O RETUBO GEZIGE RATIXU",
            ("RAXOZI GOGOTE", "RETUBO GEZIGE RATIXU"),
        ),
        ("Axa y/o Daresu y/o Fesazo", ("Axa", "Daresu", "Fesazo")),
        ("RADONO XIVOVE/ NOGIMO VAFITU", ("RADONO XIVOVE", "NOGIMO VAFITU")),
        ("/ Axa // Daresu /", ("Axa", "Daresu")),
        ("Axa Y Daresu", ("Axa", "Daresu")),
        ("Axa y Daresu y Fesazo", ("Axa y Daresu y Fesazo",)),
        ("Axa Y/O Daresu/Fesazo", ("Axa", "Daresu/Fesazo")),
        ("Axa/Daresu y Fesazo", ("Axa", "Daresu y Fesazo")),
        ("Axa/Axa", ("Axa", "Axa")),
        ("Axa (cliente antiguo)", ("Axa",)),
        ("Axa ()", ("Axa",)),
        ("(IOL)", ()),
        ("Axa (esposa Daresu)", ("Axa", "Daresu")),
        ("Axa (ESPOSO DE Daresu)", ("Axa", "Daresu")),
        ("Axa (hijo de Daresu)", ("Axa", "Daresu")),
        ("Axa (hija Fesazo)", ("Axa", "Fesazo")),
        ("Axa (hijo)", ("Axa",)),
        ("(hija de Fesazo)", ("Fesazo",)),
        ("Axa (IOL) (esposa Daresu)", ("Axa  (esposa Daresu)",)),
        ("Axa (esposa Daresu) (IOL)", ("Axa  (IOL)", "Daresu")),
        ("Axa (esposa Daresu", ("Axa (esposa Daresu",)),
    ],
)
def test_extraer_titulares_conserva_prioridades_y_heuristica(
    nombre: str, titulares: tuple[str, ...]
) -> None:
    assert extraer_titulares(nombre) == titulares


def test_extraccion_conserva_formato_de_los_nombres_y_prefijo_familiar() -> None:
    original = "  PRUEBA, Rádono / ZETA, Mízafe (esposa Gosoru)  "

    assert extraer_titulares(original) == ("PRUEBA, Rádono", "ZETA, Mízafe", "Gosoru")
