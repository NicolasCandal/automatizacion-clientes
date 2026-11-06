"""Orquestación de importación semanal sin depender de archivos o de WhatsApp."""

import logging
from dataclasses import dataclass, field
from datetime import datetime

from automatizacion_clientes.core.contratos import AlmacenSemanaContactos, ExtractorContactos, Reloj
from automatizacion_clientes.core.errores import ConfiguracionError
from automatizacion_clientes.core.importacion_whatsapp import ExtraccionContactos
from automatizacion_clientes.core.modelos import PeriodoSemanal

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class ResultadoImportacionWhatsApp:
    """Extracción validada y modo de ejecución, sin exponer identidades en repr."""

    extraccion: ExtraccionContactos = field(repr=False)
    extraido_en: datetime
    simulacion: bool


class ServicioImportacionWhatsApp:
    """Valida el lote completo y guarda solo cuando se solicita aplicar.

    No registra marcas ni conecta a Google o Excel. La salida generada se usa
    después con el flujo de coincidencias. No repite una importación completa
    automáticamente ni entrega resultados parciales si alguna entrada falla.
    """

    def __init__(
        self, *, extractor: ExtractorContactos, almacen: AlmacenSemanaContactos, reloj: Reloj
    ) -> None:
        """Recibe puertos sin ejecutar lecturas, guardados o consultas de reloj."""
        self._extractor = extractor
        self._almacen = almacen
        self._reloj = reloj

    def ejecutar(
        self, periodo: PeriodoSemanal, *, simulacion: bool = True
    ) -> ResultadoImportacionWhatsApp:
        """Extrae una semana y opcionalmente sustituye su documento de entrada.

        Args:
            periodo: Fechas inclusivas; no se deducen del reloj ni de encabezados.
            simulacion: Evita guardar archivos; inicialmente True.

        Returns:
            Extracción completa y metadatos de la ejecución.

        Raises:
            AutomatizacionError: Falló la lectura, validación o persistencia.
            ConfiguracionError: El reloj entrega un instante sin zona horaria.
        """
        extraccion = self._extractor.extraer(periodo)
        instante = self._reloj.ahora()
        if instante.tzinfo is None or instante.utcoffset() is None:
            raise ConfiguracionError("El reloj de importación debe incluir zona horaria.")
        if not simulacion:
            self._almacen.guardar(extraccion, extraido_en=instante)
        LOGGER.info(
            "Importación completada: simulación=%s; %s chats; %s contactos.",
            simulacion,
            extraccion.total_chats_escaneados,
            len(extraccion.semana.contactos),
        )
        return ResultadoImportacionWhatsApp(
            extraccion=extraccion, extraido_en=instante, simulacion=simulacion
        )
