"""Excepciones de la aplicación, independientes de los clientes y servicios.

Los adaptadores traducen sus errores técnicos a estas clases y conservan la
causa con ``raise ... from causa``. La política de logging y reintentos pertenece
a ``infra`` y a los servicios; crear una excepción no ejecuta ninguna acción.
"""

from pathlib import Path
from typing import ClassVar


class AutomatizacionError(Exception):
    """Base de los fallos esperados que puede manejar el orquestador.

    Attributes:
        CODIGO: Identificador estable para informes y registros estructurados.

    Los mensajes deben describir la operación fallida sin incluir credenciales
    ni datos de clientes. Esta clase no sanea el texto recibido ni la causa
    técnica; los adaptadores son responsables de construir mensajes adecuados.
    """

    CODIGO: ClassVar[str] = "AUTOMATIZACION"


class DominioError(AutomatizacionError):
    """Una regla de negocio o una condición del estado impide continuar."""

    CODIGO: ClassVar[str] = "DOMINIO"


class ValidacionError(DominioError, ValueError):
    """Un valor o una relación viola las invariantes de un modelo.

    Mantiene compatibilidad con los manejadores de ``ValueError`` de los modelos
    y permite capturar todos los fallos de negocio mediante ``DominioError``.
    """

    CODIGO: ClassVar[str] = "VALIDACION"


class EstructuraError(DominioError):
    """El origen no tiene las hojas, columnas o formato requeridos."""

    CODIGO: ClassVar[str] = "ESTRUCTURA"


class ConcurrenciaError(DominioError):
    """El origen cambió y debe volver a leerse antes de planificar la escritura.

    Repetir el mismo plan no resuelve este conflicto. El servicio debe obtener
    una nueva revisión y decidir cómo proceder.
    """

    CODIGO: ClassVar[str] = "CONCURRENCIA"


class ConfiguracionError(AutomatizacionError):
    """La ejecución requiere corregir opciones de configuración."""

    CODIGO: ClassVar[str] = "CONFIGURACION"


class InfraestructuraError(AutomatizacionError):
    """Una operación con archivos o servicios externos no pudo completarse."""

    CODIGO: ClassVar[str] = "INFRAESTRUCTURA"


class AutenticacionError(InfraestructuraError):
    """Las credenciales faltan, son inválidas o requieren autorización del usuario."""

    CODIGO: ClassVar[str] = "AUTENTICACION"


class AutorizacionError(InfraestructuraError):
    """La identidad autenticada no tiene permisos para la operación solicitada."""

    CODIGO: ClassVar[str] = "AUTORIZACION"


class PersistenciaError(InfraestructuraError):
    """No se pudo leer, guardar o respaldar un archivo local."""

    CODIGO: ClassVar[str] = "PERSISTENCIA"


class ServicioExternoError(InfraestructuraError):
    """Un servicio falló sin evidencia suficiente para clasificarlo como transitorio."""

    CODIGO: ClassVar[str] = "SERVICIO_EXTERNO"


class TransitorioError(ServicioExternoError):
    """Un fallo externo puede desaparecer al repetir una operación segura.

    El adaptador debe clasificar el fallo original. Esta clase permite
    seleccionar excepciones para Tenacity, pero no demuestra que una escritura
    sea idempotente ni autoriza reintentar operaciones con efectos duplicables.
    """

    CODIGO: ClassVar[str] = "TRANSITORIO"


class TiempoEsperaError(TransitorioError):
    """Una operación excedió el tiempo de espera y su resultado puede ser incierto.

    Antes de repetir una escritura, el servicio debe comprobar su resultado o
    apoyarse en la idempotencia del adaptador.
    """

    CODIGO: ClassVar[str] = "TIEMPO_ESPERA"


class RegistroInterrumpidoError(AutomatizacionError):
    """Fallo posterior al respaldo, con información para revisar la recuperación.

    ``hojas_confirmadas`` contiene solo escrituras verificadas. La hoja en curso
    puede haberse modificado aunque fallara su llamada o la verificación. No se
    restaura ni se reintenta automáticamente; la causa original queda encadenada.
    """

    CODIGO: ClassVar[str] = "REGISTRO_INTERRUMPIDO"

    def __init__(
        self,
        *,
        respaldo: Path | str,
        hojas_confirmadas: tuple[str, ...],
        hoja_en_curso: str | None,
    ) -> None:
        """Conserva el estado de recuperación sin exponerlo en el mensaje público."""
        super().__init__(
            "Registro interrumpido. Revise el origen y el respaldo antes de continuar."
        )
        self.respaldo = respaldo
        self.hojas_confirmadas = hojas_confirmadas
        self.hoja_en_curso = hoja_en_curso
