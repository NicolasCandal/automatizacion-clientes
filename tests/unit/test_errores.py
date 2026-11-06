"""Contrato de clasificación de fallos para el orquestador y los reintentos."""

from datetime import date

import pytest

from automatizacion_clientes.core.errores import (
    AutenticacionError,
    AutomatizacionError,
    AutorizacionError,
    ConcurrenciaError,
    ConfiguracionError,
    DominioError,
    EstructuraError,
    InfraestructuraError,
    PersistenciaError,
    ServicioExternoError,
    TiempoEsperaError,
    TransitorioError,
    ValidacionError,
)
from automatizacion_clientes.core.modelos import PeriodoSemanal


def test_modelos_emiten_error_de_dominio_compatible_con_value_error() -> None:
    with pytest.raises(AutomatizacionError) as captura:
        PeriodoSemanal(desde=date(2026, 11, 6), hasta=date(2026, 11, 2))

    assert isinstance(captura.value, ValidacionError)
    assert isinstance(captura.value, DominioError)
    assert isinstance(captura.value, ValueError)
    assert captura.value.CODIGO == "VALIDACION"
    assert not isinstance(captura.value, InfraestructuraError)


@pytest.mark.parametrize("tipo_error", [TransitorioError, TiempoEsperaError])
def test_fallos_temporales_se_pueden_capturar_como_servicio_o_infraestructura(
    tipo_error: type[TransitorioError],
) -> None:
    error = tipo_error("El servicio no respondió a tiempo.")

    assert isinstance(error, AutomatizacionError)
    assert isinstance(error, ServicioExternoError)
    assert isinstance(error, InfraestructuraError)
    assert not isinstance(error, DominioError)


@pytest.mark.parametrize(
    "tipo_error",
    [
        ValidacionError,
        EstructuraError,
        ConcurrenciaError,
        ConfiguracionError,
        AutenticacionError,
        AutorizacionError,
        PersistenciaError,
        ServicioExternoError,
    ],
)
def test_fallos_sin_clasificacion_temporal_no_entran_en_reintentos(
    tipo_error: type[AutomatizacionError],
) -> None:
    error = tipo_error("La operación requiere revisión.")

    assert not isinstance(error, TransitorioError)


def test_autenticacion_y_permisos_se_pueden_tratar_por_separado() -> None:
    sin_identidad = AutenticacionError("Se necesita autorizar el acceso.")
    sin_permisos = AutorizacionError("No hay permisos para la operación.")

    assert isinstance(sin_identidad, InfraestructuraError)
    assert isinstance(sin_permisos, InfraestructuraError)
    assert not isinstance(sin_identidad, AutorizacionError)
    assert not isinstance(sin_permisos, AutenticacionError)


@pytest.mark.parametrize("tipo_error", [TypeError, KeyError, RuntimeError, ValueError, OSError])
def test_manejo_de_errores_esperados_no_captura_fallos_sin_traducir(
    tipo_error: type[Exception],
) -> None:
    assert not isinstance(tipo_error("Fallo técnico sin traducir."), AutomatizacionError)


def test_categorias_tienen_codigos_distintos_para_informes() -> None:
    categorias = (
        AutomatizacionError,
        DominioError,
        ValidacionError,
        EstructuraError,
        ConcurrenciaError,
        ConfiguracionError,
        InfraestructuraError,
        AutenticacionError,
        AutorizacionError,
        PersistenciaError,
        ServicioExternoError,
        TransitorioError,
        TiempoEsperaError,
    )

    codigos = tuple(categoria.CODIGO for categoria in categorias)

    assert len(set(codigos)) == len(categorias)
    assert all(codigo and codigo.isupper() for codigo in codigos)
