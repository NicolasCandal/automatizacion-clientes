"""Casos de comparación de nombres contra la biblioteca real, sin red."""

from math import isfinite

import pytest

from automatizacion_clientes.core.contratos import ComparadorNombres
from automatizacion_clientes.core.normalizacion import normalizar
from automatizacion_clientes.infra.comparador_rapidfuzz import ComparadorRapidFuzz


@pytest.fixture
def comparador() -> ComparadorNombres:
    return ComparadorRapidFuzz()


@pytest.mark.parametrize(
    ("consulta", "candidato", "esperado"),
    [
        ("axa prueba", "axa prueba", 100.0),
        ("prueba axa", "axa prueba", 100.0),
        ("firuso fivora", "firuso baleve fivora", 100.0),
        ("firuso baleve fivora", "firuso fivora", 100.0),
        ("axa axa prueba", "axa prueba", 100.0),
        ("aaa", "zzz", 0.0),
        ("axa", "axxa", 85.71428571428571),
        ("axa", "axaxa", 75.0),
        ("axefor", "axxefor", 92.3076923076923),
        ("", "", 0.0),
        ("", "axa prueba", 0.0),
        ("axa prueba", "", 0.0),
        (" \t ", " \n ", 0.0),
    ],
)
def test_puntuaciones_conocidas_conservan_escala_y_precision(
    comparador: ComparadorNombres, consulta: str, candidato: str, esperado: float
) -> None:
    puntuacion = comparador.similitud(consulta, candidato)

    assert isinstance(puntuacion, float)
    assert isfinite(puntuacion)
    assert puntuacion == pytest.approx(esperado)


def test_normalizacion_pertenece_al_dominio(comparador: ComparadorNombres) -> None:
    assert comparador.similitud("AXA", "axa") == 0.0
    assert (
        comparador.similitud(
            normalizar("PRUEBA, Rádono Mízafe"), normalizar("Mizafe Radono Prueba")
        )
        == 100.0
    )
