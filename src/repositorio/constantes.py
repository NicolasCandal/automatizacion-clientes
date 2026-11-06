"""Constantes históricas derivadas de la estructura actual."""

from automatizacion_clientes.infra.lector_clientes_excel import (
    COLOR_VERDE_RGB,
)
from automatizacion_clientes.infra.lector_clientes_excel import (
    FILA_ENCABEZADO_SEMANA as FILA_ENCABEZADO_SEMANA,
)
from automatizacion_clientes.infra.lector_clientes_excel import (
    HOJAS_ESPERADAS as HOJAS_ESPERADAS,
)

COL_NOMBRE = 1
VERDE_XLSX = "FF" + COLOR_VERDE_RGB
VERDE_SHEETS = {"red": 118 / 255, "green": 146 / 255, "blue": 60 / 255}
BLANCO_XLSX = "FFFFFFFF"
BLANCO_SHEETS = {"red": 1.0, "green": 1.0, "blue": 1.0}
TOLERANCIA_COLOR = 0.01
MARCA_HISTORICA_TEXTO = "x"
