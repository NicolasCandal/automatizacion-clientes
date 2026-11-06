"""Interpretación pura de encabezados sintéticos y filtrado inclusivo de fechas."""

from datetime import date
from typing import cast

import pytest

from automatizacion_clientes.core.errores import EstructuraError, ValidacionError
from automatizacion_clientes.core.importacion_whatsapp import (
    ExtraccionContactos,
    OrdenFecha,
    analizar_actividad,
)
from automatizacion_clientes.core.modelos import PeriodoSemanal, SemanaContactos

PERIODO = PeriodoSemanal(desde=date(2026, 11, 2), hasta=date(2026, 11, 6))


@pytest.mark.parametrize(
    "cabecera",
    [
        "02/11/2026, 09:15 - Axa: contenido privado",
        "2/11/2026 09:15 - Axa: contenido privado",
        "[02/11/2026, 09:15:31] Axa: contenido privado",
        "[02/11/2026, 9:15 PM] Axa: contenido privado",
        "02/11/2026, 9:15 p. m. - Axa: contenido privado",
        "[02/11/2026, 9:15:00\u202fp.\u00a0m.] Axa: contenido privado",
        "02.11.2026, 00:00 - Axa:",
        "02-11-2026, 23:59:59 - Axa: <Multimedia omitido>",
        "\ufeff\u200e[2026-11-02, 12:00 AM] \u200eAxa: contenido privado\r\n",
    ],
)
def test_encabezados_admitidos_no_retienen_cuerpo(cabecera: str) -> None:
    resultado = analizar_actividad(
        [cabecera], periodo=PERIODO, remitentes=("Axa",), orden_fecha="dia_mes"
    )
    assert resultado.ultima_fecha == date(2026, 11, 2)
    assert resultado.mensajes_reconocidos == resultado.mensajes_en_periodo == 1
    assert "privado" not in repr(resultado)
    assert "Axa" not in repr(resultado)


def test_ultima_fecha_se_calcula_dentro_del_periodo_no_del_chat_completo() -> None:
    resultado = analizar_actividad(
        [
            "01/11/2026, 12:00 - Axa: previo",
            "06/11/2026, 12:00 - Yo: dentro",
            "03/11/2026, 12:00 - Axa: fuera de orden",
            "20/11/2026, 12:00 - Axa: posterior",
        ],
        periodo=PERIODO,
        remitentes=("Axa", "Yo"),
        orden_fecha="dia_mes",
    )
    assert resultado.ultima_fecha == date(2026, 11, 6)
    assert resultado.mensajes_reconocidos == 4
    assert resultado.mensajes_en_periodo == 2


def test_avisos_remitentes_no_seleccionados_y_multilinea_no_cuentan() -> None:
    resultado = analizar_actividad(
        [
            "\n",
            "02/11/2026, 12:00 - Los mensajes están cifrados de extremo a extremo.",
            "02/11/2026, 12:00 - Axa: primera línea",
            "continuación: sin nuevo mensaje",
            "\n",
            "05/11/2026, 12:00 - Otro: no seleccionado",
            "06/11/2026, 12:00 - Se cambió la descripción: texto de sistema",
        ],
        periodo=PERIODO,
        remitentes=("Axa",),
        orden_fecha="dia_mes",
    )
    assert resultado.ultima_fecha == date(2026, 11, 2)
    assert resultado.mensajes_reconocidos == 1


def test_todo_fuera_del_periodo_entrega_fecha_nula() -> None:
    resultado = analizar_actividad(
        ["01/11/2026, 12:00 - Axa: previo"],
        periodo=PERIODO,
        remitentes=("Axa",),
        orden_fecha="dia_mes",
    )
    assert resultado.ultima_fecha is None
    assert resultado.mensajes_en_periodo == 0
    assert resultado.mensajes_reconocidos == 1


def test_orden_mes_dia_y_siglo_son_explicitos() -> None:
    resultado = analizar_actividad(
        ["11/02/26, 12:00 - Axa: texto"],
        periodo=PERIODO,
        remitentes=("Axa",),
        orden_fecha="mes_dia",
        siglo_anio_corto=2000,
    )
    assert resultado.ultima_fecha == date(2026, 11, 2)
    historico = analizar_actividad(
        ["11/02/26, 12:00 - Axa: texto"],
        periodo=PERIODO,
        remitentes=("Axa",),
        orden_fecha="mes_dia",
        siglo_anio_corto=1900,
    )
    assert historico.ultima_fecha is None


@pytest.mark.parametrize(
    "cabecera",
    [
        "31/11/2026, 12:00 - Axa: secreto",
        "02/13/2026, 12:00 - Axa: secreto",
        "02/11/26, 12:00 - Axa: secreto",
        "02/11/026, 12:00 - Axa: secreto",
        "02/11-2026, 12:00 - Axa: secreto",
        "2026/11/02, 12:00 - Axa: secreto",
        "02/11/2026, 24:00 - Axa: secreto",
        "02/11/2026, 10:60 - Axa: secreto",
        "02/11/2026, 10:10:60 - Axa: secreto",
        "02/11/2026, 00:10 AM - Axa: secreto",
        "02/11/2026, 13:10 PM - Axa: secreto",
    ],
)
def test_fechas_horas_y_anios_ambiguos_se_rechazan(cabecera: str) -> None:
    with pytest.raises(ValidacionError) as error:
        analizar_actividad([cabecera], periodo=PERIODO, remitentes=("Axa",), orden_fecha="dia_mes")
    assert "secreto" not in str(error.value)


@pytest.mark.parametrize(
    "lineas",
    [
        [],
        ["\n"],
        ["archivo ajeno"],
        ["02/11/2026, xx:00 - Axa: secreto"],
        ["02/11/2026, 12:00 - Axa: válido", "03/11/2026, 12:xx - Axa: secreto"],
        ["02/11/2026, 12:00 - Axa: texto\r03/11/2026, 12:00 - Axa: otro"],
    ],
)
def test_exportaciones_vacias_o_incompatibles_no_generan_parciales(lineas: list[str]) -> None:
    with pytest.raises(EstructuraError):
        analizar_actividad(lineas, periodo=PERIODO, remitentes=("Axa",), orden_fecha="dia_mes")


@pytest.mark.parametrize(
    "remitentes", [(), ("",), (" Axa",), ("Axa", "Axa"), ("Axa\nYo",), ("Axa: Empresa",)]
)
def test_remitentes_invalidos_se_rechazan(remitentes: tuple[str, ...]) -> None:
    with pytest.raises(ValidacionError):
        analizar_actividad([], periodo=PERIODO, remitentes=remitentes, orden_fecha="dia_mes")


@pytest.mark.parametrize("siglo", [1800, 2020, True])
def test_siglo_no_admitido_se_rechaza(siglo: int) -> None:
    with pytest.raises(ValidacionError):
        analizar_actividad(
            [], periodo=PERIODO, remitentes=("Axa",), orden_fecha="dia_mes", siglo_anio_corto=siglo
        )


def test_no_se_acepta_orden_inventado_o_remitentes_sin_actividad() -> None:
    with pytest.raises(ValidacionError):
        analizar_actividad(
            [], periodo=PERIODO, remitentes=("Axa",), orden_fecha=cast(OrdenFecha, "automatico")
        )
    with pytest.raises(ValidacionError):
        analizar_actividad(
            ["02/11/2026, 12:00 - Otro: texto"],
            periodo=PERIODO,
            remitentes=("Axa",),
            orden_fecha="dia_mes",
        )


@pytest.mark.parametrize("cantidad", [-1, True])
def test_cantidades_de_extraccion_son_enteros_no_negativos(cantidad: int) -> None:
    with pytest.raises(ValidacionError):
        ExtraccionContactos(
            semana=SemanaContactos(periodo=PERIODO, contactos=()),
            total_chats_escaneados=cantidad,
            mensajes_reconocidos=0,
        )
