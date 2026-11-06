"""Interpretación pura de actividad en exportaciones de texto de WhatsApp."""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from .errores import EstructuraError, ValidacionError
from .modelos import PeriodoSemanal, SemanaContactos

type OrdenFecha = Literal["dia_mes", "mes_dia"]

_FECHA = r"(?P<fecha>[0-9]{1,4}[/.-][0-9]{1,2}[/.-][0-9]{1,4})"
_HORA = (
    r"(?P<hora>[0-9]{1,2}):(?P<minuto>[0-9]{2})(?::(?P<segundo>[0-9]{2}))?"
    r"(?:\s*(?P<meridiano>[ap]\.?\s*m\.?))?"
)
_ANDROID = re.compile(r"^" + _FECHA + r",?\s+" + _HORA + r"\s+-\s+(?P<resto>.*)$", re.IGNORECASE)
_IOS = re.compile(r"^\[" + _FECHA + r",?\s+" + _HORA + r"\]\s+(?P<resto>.*)$", re.IGNORECASE)
_PARECE_FECHADO = re.compile(r"^\[?[0-9]{1,4}[/.-][0-9]{1,2}[/.-][0-9]{1,4}(?:,|\s)")


@dataclass(frozen=True, slots=True, kw_only=True)
class ActividadChat:
    """Actividad reconocida de un archivo, sin contenido ni nombres de remitentes."""

    ultima_fecha: date | None
    mensajes_reconocidos: int
    mensajes_en_periodo: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ExtraccionContactos:
    """Entrada semanal y cantidades del lote de chats analizado por completo."""

    semana: SemanaContactos = field(repr=False)
    total_chats_escaneados: int
    mensajes_reconocidos: int

    def __post_init__(self) -> None:
        """Evita metadatos negativos o incompatibles con los contactos extraídos."""
        for cantidad in (self.total_chats_escaneados, self.mensajes_reconocidos):
            if type(cantidad) is not int or cantidad < len(self.semana.contactos):
                raise ValidacionError("Las cantidades de extracción no corresponden a la semana.")


def analizar_actividad(
    lineas: Iterable[str],
    *,
    periodo: PeriodoSemanal,
    remitentes: tuple[str, ...],
    orden_fecha: OrdenFecha,
    siglo_anio_corto: int | None = None,
) -> ActividadChat:
    """Obtiene la última fecha dentro del período sin retener cuerpos de mensajes.

    Admite encabezados con separador Android o corchetes iOS, horas de 24 o 12
    horas y segundos opcionales. El orden día/mes y el siglo de años cortos son
    explícitos. Las fechas se interpretan tal como aparecen, sin convertir zona.
    Solo cuentan encabezados de los remitentes declarados; los demás eventos
    fechados y las continuaciones de mensajes no cuentan como actividad.

    Args:
        lineas: Texto completo del archivo, leído secuencialmente por el adaptador.
        periodo: Intervalo inclusivo de fechas de calendario.
        remitentes: Etiquetas exactas que contarán como actividad, sin duplicados.
        orden_fecha: Interpretación explícita de fechas que no empiezan por el año.
        siglo_anio_corto: 1900 o 2000 para años de dos dígitos; None los rechaza.

    Returns:
        Última fecha reconocida dentro del período, o None si no hubo actividad.

    Raises:
        EstructuraError: Exportación vacía o encabezado fechado incompatible.
        ValidacionError: Fechas, horas u opciones inválidas, o ningún remitente reconocido.
    """
    if (
        orden_fecha not in {"dia_mes", "mes_dia"}
        or not remitentes
        or len(set(remitentes)) != len(remitentes)
        or any(
            not nombre
            or nombre != nombre.strip()
            or "\n" in nombre
            or "\r" in nombre
            or ": " in nombre
            for nombre in remitentes
        )
        or (
            siglo_anio_corto is not None
            and (type(siglo_anio_corto) is not int or siglo_anio_corto not in {1900, 2000})
        )
    ):
        raise ValidacionError("Configure remitentes, orden de fecha y siglo válidos.")
    etiquetas = frozenset(remitentes)
    ultima: date | None = None
    mensajes = 0
    incluidos = 0
    encabezados = 0
    for linea in lineas:
        texto = linea.rstrip("\r\n").lstrip("\ufeff\u200e\u200f ")
        if "\r" in texto or "\n" in texto:
            raise EstructuraError("Cada entrada debe contener una sola línea LF o CRLF.")
        coincidencia = _ANDROID.match(texto) or _IOS.match(texto)
        if coincidencia is None:
            if _PARECE_FECHADO.match(texto) or (not encabezados and texto.strip()):
                raise EstructuraError("La exportación contiene un encabezado no admitido.")
            continue
        encabezados += 1
        fecha = _leer_fecha(coincidencia["fecha"], orden_fecha, siglo_anio_corto)
        _validar_hora(coincidencia)
        resto = coincidencia["resto"].lstrip("\u200e\u200f")
        separador = resto.find(": ")
        etiqueta = (
            resto[:separador] if separador >= 0 else resto[:-1] if resto.endswith(":") else ""
        )
        if etiqueta not in etiquetas:
            continue
        mensajes += 1
        if periodo.contiene(fecha):
            incluidos += 1
            ultima = fecha if ultima is None else max(ultima, fecha)
    if not encabezados:
        raise EstructuraError("La exportación no contiene encabezados admitidos.")
    if not mensajes:
        raise ValidacionError("No se reconoció actividad de los remitentes configurados.")
    return ActividadChat(
        ultima_fecha=ultima, mensajes_reconocidos=mensajes, mensajes_en_periodo=incluidos
    )


def _leer_fecha(texto: str, orden: OrdenFecha, siglo: int | None) -> date:
    if len(set(re.findall(r"[/.-]", texto))) != 1:
        raise ValidacionError("La fecha exportada mezcla separadores incompatibles.")
    partes = re.split(r"[/.-]", texto)
    if len(partes[0]) == 4 and texto.count("-") == 2:
        anio, mes, dia = (int(parte) for parte in partes)
    else:
        if len(partes[0]) > 2 or len(partes[1]) > 2 or len(partes[2]) not in {2, 4}:
            raise ValidacionError("La fecha exportada tiene un formato no admitido.")
        primero, segundo, anio = (int(parte) for parte in partes)
        dia, mes = (primero, segundo) if orden == "dia_mes" else (segundo, primero)
        if len(partes[2]) == 2:
            if siglo is None:
                raise ValidacionError("Los años de dos dígitos requieren un siglo explícito.")
            anio += siglo
    try:
        return date(anio, mes, dia)
    except ValueError as causa:
        raise ValidacionError("La exportación contiene una fecha inválida.") from causa


def _validar_hora(coincidencia: re.Match[str]) -> None:
    hora, minuto = int(coincidencia["hora"]), int(coincidencia["minuto"])
    segundo = int(coincidencia["segundo"] or "0")
    meridiano = coincidencia["meridiano"]
    if (
        not 0 <= minuto <= 59
        or not 0 <= segundo <= 59
        or not (1 <= hora <= 12 if meridiano else 0 <= hora <= 23)
    ):
        raise ValidacionError("La exportación contiene una hora inválida.")
