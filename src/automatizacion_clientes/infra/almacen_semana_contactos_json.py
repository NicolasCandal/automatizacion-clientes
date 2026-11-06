"""Salida semanal compatible con el lector, sin contenido de conversaciones."""

import logging
from datetime import UTC, datetime
from pathlib import Path

from automatizacion_clientes.core.errores import ConfiguracionError, ValidacionError
from automatizacion_clientes.core.importacion_whatsapp import ExtraccionContactos
from automatizacion_clientes.infra.almacenamiento_json import ValorJson, guardar_json

LOGGER = logging.getLogger(__name__)


class AlmacenSemanaContactosJson:
    """Implementa ``AlmacenSemanaContactos`` mediante reemplazo atómico JSON.

    El manifiesto se declara como entrada protegida. El destino debe ser JSON;
    nunca sustituye una exportación TXT ni el manifiesto utilizado. Se requiere
    un único escritor: el reemplazo no constituye un bloqueo entre procesos.
    """

    def __init__(self, ruta: Path, *, manifiesto: Path) -> None:
        """Valida destinos sin abrir archivos ni crear directorios."""
        self._ruta = ruta.expanduser().resolve()
        entrada = manifiesto.expanduser().resolve()
        if self._ruta.suffix.lower() != ".json" or (
            self._ruta.is_relative_to(entrada) or entrada.is_relative_to(self._ruta)
        ):
            raise ConfiguracionError(
                "El destino semanal debe ser JSON e independiente del manifiesto."
            )

    def guardar(self, extraccion: ExtraccionContactos, *, extraido_en: datetime) -> None:
        """Guarda identidades, fechas y contadores, conservando la salida ante fallos.

        Args:
            extraccion: Lote completo ya validado, incluidos grupos y chats sin actividad.
            extraido_en: Instante de extracción con zona horaria.

        Raises:
            ValidacionError: El reloj no entrega un instante consciente de zona.
            PersistenciaError: Falló el guardado; no se sustituye parcialmente el destino.
        """
        if extraido_en.tzinfo is None or extraido_en.utcoffset() is None:
            raise ValidacionError("El instante de extracción debe incluir zona horaria.")
        semana = extraccion.semana
        contactos: list[ValorJson] = [
            {
                "nombre_crudo": contacto.nombre_crudo,
                "ultima_fecha": contacto.ultima_fecha.isoformat(),
                "telefono": contacto.telefono,
                "es_grupo": contacto.es_grupo,
            }
            for contacto in semana.contactos
        ]
        documento: dict[str, ValorJson] = {
            "rango": {
                "desde": semana.periodo.desde.isoformat(),
                "hasta": semana.periodo.hasta.isoformat(),
            },
            "contactos": contactos,
            "extraido_en": extraido_en.astimezone(UTC).isoformat(),
            "total_chats_escaneados": extraccion.total_chats_escaneados,
        }
        guardar_json(self._ruta, documento)
        LOGGER.info("Entrada semanal guardada: %s contactos.", len(semana.contactos))
