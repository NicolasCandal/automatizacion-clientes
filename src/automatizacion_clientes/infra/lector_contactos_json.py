"""Lectura de semana.json y conversión estricta a los modelos del dominio."""

import logging
from datetime import date, datetime
from pathlib import Path

from automatizacion_clientes.core.errores import EstructuraError, ValidacionError
from automatizacion_clientes.core.modelos import ContactoSemanal, PeriodoSemanal, SemanaContactos
from automatizacion_clientes.infra.almacenamiento_json import ValorJson, leer_json

LOGGER = logging.getLogger(__name__)


class LectorContactosJson:
    """Implementa ``LectorContactos`` para el formato documentado del extractor.

    Conserva nombres, teléfonos, orden, grupos y contactos repetidos. No infiere
    países ni modifica el formato telefónico: debe coincidir con el de aliases.
    El filtrado de grupos y la resolución de identidades pertenecen al motor.

    Exige ``rango`` y ``contactos``. Admite los metadatos ``extraido_en`` y
    ``total_chats_escaneados``; los valida cuando existen, pero no los incorpora
    al modelo de negocio. Rechaza campos desconocidos para detectar cambios de
    esquema. No acepta contenido de mensajes.
    """

    def __init__(self, ruta: Path) -> None:
        """Recibe la ruta configurada sin acceder al archivo ni crear carpetas.

        Args:
            ruta: Documento semanal, normalmente ``Config.contactos_path``.
        """
        self._ruta = ruta

    def leer(self) -> SemanaContactos:
        """Lee y valida el documento completo antes de devolver una semana.

        Cada llamada vuelve a leer el archivo. No descarta entradas inválidas
        ni entrega resultados parciales si falla la validación de un contacto.

        Returns:
            Período inclusivo y todos los contactos, con fechas de calendario.

        Raises:
            EstructuraError: JSON inválido, campos ausentes o desconocidos, o
                tipos incompatibles con el esquema.
            ValidacionError: Fechas no canónicas, período invertido, contactos
                fuera del período, identidades vacías o metadatos inconsistentes.
            PersistenciaError: No se puede leer el archivo, incluido si no existe.
        """
        documento = leer_json(self._ruta)
        if not isinstance(documento, dict) or not {"rango", "contactos"} <= documento.keys() <= {
            "rango",
            "contactos",
            "extraido_en",
            "total_chats_escaneados",
        }:
            raise EstructuraError("El documento semanal contiene campos ausentes o desconocidos.")
        rango = documento["rango"]
        if not isinstance(rango, dict) or rango.keys() != {"desde", "hasta"}:
            raise EstructuraError("El rango semanal debe contener únicamente desde y hasta.")
        periodo = PeriodoSemanal(
            desde=_leer_fecha(rango["desde"]), hasta=_leer_fecha(rango["hasta"])
        )
        contactos = documento["contactos"]
        if not isinstance(contactos, list):
            raise EstructuraError("Los contactos semanales deben ser una lista JSON.")
        _validar_metadatos(documento, len(contactos))
        semana = SemanaContactos(
            periodo=periodo,
            contactos=tuple(_decodificar_contacto(contacto) for contacto in contactos),
        )
        LOGGER.info("Entrada semanal validada: %s contactos.", len(semana.contactos))
        return semana


def _decodificar_contacto(entrada: ValorJson) -> ContactoSemanal:
    if not isinstance(entrada, dict) or not {"nombre_crudo", "ultima_fecha"} <= entrada.keys() <= {
        "nombre_crudo",
        "ultima_fecha",
        "telefono",
        "es_grupo",
    }:
        raise EstructuraError("El contacto contiene campos ausentes o desconocidos.")
    nombre = entrada["nombre_crudo"]
    telefono = entrada.get("telefono")
    es_grupo = entrada.get("es_grupo", False)
    if (
        not isinstance(nombre, str)
        or not (telefono is None or isinstance(telefono, str))
        or not isinstance(es_grupo, bool)
    ):
        raise EstructuraError("Los tipos de los campos del contacto no son válidos.")
    return ContactoSemanal(
        nombre_crudo=nombre,
        ultima_fecha=_leer_fecha(entrada["ultima_fecha"]),
        telefono=telefono,
        es_grupo=es_grupo,
    )


def _leer_fecha(valor: ValorJson) -> date:
    if not isinstance(valor, str):
        raise EstructuraError("Las fechas semanales deben ser cadenas YYYY-MM-DD.")
    try:
        fecha = date.fromisoformat(valor)
    except ValueError as causa:
        raise ValidacionError("La fecha semanal no es una fecha válida YYYY-MM-DD.") from causa
    if fecha.isoformat() != valor:
        raise ValidacionError("La fecha semanal debe usar exactamente el formato YYYY-MM-DD.")
    return fecha


def _validar_metadatos(documento: dict[str, ValorJson], cantidad_contactos: int) -> None:
    if "extraido_en" in documento:
        instante = documento["extraido_en"]
        if not isinstance(instante, str):
            raise EstructuraError(
                "La fecha de extracción debe ser una cadena ISO con zona horaria."
            )
        try:
            fecha_extraccion = datetime.fromisoformat(instante)
        except ValueError as causa:
            raise ValidacionError("La fecha de extracción no es un instante ISO válido.") from causa
        if fecha_extraccion.utcoffset() is None:
            raise ValidacionError("La fecha de extracción debe incluir zona horaria.")
    if "total_chats_escaneados" in documento:
        total = documento["total_chats_escaneados"]
        if type(total) is not int:
            raise EstructuraError("El total de chats escaneados debe ser un entero.")
        if total < cantidad_contactos:
            raise ValidacionError(
                "El total de chats escaneados no puede ser menor que los contactos."
            )
