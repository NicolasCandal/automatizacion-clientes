"""Guardas de las interfaces usadas para simular adaptadores en los servicios."""

from unittest.mock import create_autospec

import pytest

from automatizacion_clientes.core.contratos import EscritorClientes, LectorClientes


def test_escritura_exige_revision_como_argumento_explicito() -> None:
    escritor = create_autospec(EscritorClientes, instance=True, spec_set=True)
    filas = frozenset({3, 20})

    with pytest.raises(TypeError, match="revision_esperada"):
        escritor.aplicar_marcas("IOL", 10, "02 al 06-11", filas)
    with pytest.raises(TypeError):
        escritor.aplicar_marcas("IOL", 10, "02 al 06-11", filas, "revision_1")

    escritor.aplicar_marcas("IOL", 10, "02 al 06-11", filas, revision_esperada="revision_1")

    escritor.aplicar_marcas.assert_called_once_with(
        "IOL", 10, "02 al 06-11", filas, revision_esperada="revision_1"
    )
    escritor.crear_respaldo.assert_not_called()


def test_doble_de_lectura_impide_usar_operaciones_de_escritura() -> None:
    lector = create_autospec(LectorClientes, instance=True, spec_set=True)

    with pytest.raises(AttributeError, match="aplicar_marcas"):
        lector.aplicar_marcas("IOL", 10, "02 al 06-11", frozenset(), revision_esperada="revision_1")
    with pytest.raises(AttributeError, match="crear_respaldo"):
        lector.crear_respaldo()
