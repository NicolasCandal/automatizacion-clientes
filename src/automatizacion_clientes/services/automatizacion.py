"""Flujo completo que comparte instantáneas entre catálogo, resolución y registro."""

import logging
from dataclasses import dataclass, field

from automatizacion_clientes.core.contratos import (
    AlmacenCatalogo,
    AlmacenResoluciones,
    LectorContactos,
)
from automatizacion_clientes.core.errores import ConcurrenciaError
from automatizacion_clientes.core.modelos import ResolucionSemanal
from automatizacion_clientes.core.registro import ResultadoRegistro
from automatizacion_clientes.services.catalogo import ServicioCatalogo
from automatizacion_clientes.services.coincidencias import ServicioCoincidencias
from automatizacion_clientes.services.registro import ServicioRegistro

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class ResultadoAutomatizacion:
    """Informe del flujo; sin registro cuando los pendientes impiden aplicarlo."""

    simulacion: bool
    resolucion: ResolucionSemanal = field(repr=False)
    registro: ResultadoRegistro | None = field(repr=False)


class ServicioAutomatizacion:
    """Coordina los servicios sin abrir archivos ni construir sus adaptadores.

    La simulación no persiste nada. En ejecución real, el catálogo y el informe
    se guardan antes del registro; no hay transacción entre archivos y planilla.
    Las decisiones se previsualizan antes de aprender aliases y se contrastan
    con la resolución persistida antes de permitir una escritura de clientes.
    """

    def __init__(
        self,
        *,
        catalogo: ServicioCatalogo,
        coincidencias: ServicioCoincidencias,
        registro: ServicioRegistro,
        lector_contactos: LectorContactos,
        almacen_catalogo: AlmacenCatalogo,
        almacen_resoluciones: AlmacenResoluciones,
    ) -> None:
        """Recibe servicios y sus almacenes compartidos, sin ejecutar operaciones."""
        self._catalogo = catalogo
        self._coincidencias = coincidencias
        self._registro = registro
        self._contactos = lector_contactos
        self._almacen_catalogo = almacen_catalogo
        self._informes = almacen_resoluciones

    def ejecutar(
        self,
        *,
        encabezado: str,
        simulacion: bool = True,
        permitir_pendientes: bool = False,
        forzar_catalogo: bool = False,
    ) -> ResultadoAutomatizacion:
        """Resuelve sobre una única extracción y registra solo tras planificar.

        Args:
            encabezado: Identificador explícito del período de contactos.
            simulacion: Evita todas las escrituras, incluidos informes y aliases.
            permitir_pendientes: Autoriza el reemplazo parcial con solo resueltos.
            forzar_catalogo: Reconstruye la caché, incluso si su formato es inválido.

        Returns:
            Resolución y registro, o resolución sin registro si hay pendientes.

        Raises:
            AutomatizacionError: Fallo en cualquier fase. El catálogo, informe o
                aliases pueden haber sido guardados antes de fallar el registro.
            RegistroInterrumpidoError: Falló una operación posterior al respaldo.
        """
        catalogo = self._catalogo.ejecutar(simulacion=True, forzar=forzar_catalogo)
        semana = self._contactos.leer()
        resolucion = self._coincidencias.ejecutar(catalogo=catalogo, semana=semana)
        if resolucion.pendientes and not permitir_pendientes:
            if not simulacion:
                self._almacen_catalogo.guardar(catalogo)
                self._informes.guardar(resolucion)
            LOGGER.warning(
                "Registro pendiente de revisión: %s resultados pendientes; sin marcas ni aliases.",
                len(resolucion.pendientes),
            )
            return ResultadoAutomatizacion(
                simulacion=simulacion, resolucion=resolucion, registro=None
            )
        plan = self._registro.planificar(
            catalogo=catalogo,
            semana=semana,
            resolucion=resolucion,
            encabezado=encabezado,
            permitir_pendientes=permitir_pendientes,
        )
        for operacion, anteriores in zip(plan.operaciones, plan.marcas_anteriores, strict=True):
            LOGGER.info(
                "Plan de hoja: %s; columna=%s; nueva=%s; marcar=%s; agregar=%s; quitar=%s.",
                operacion.hoja,
                operacion.columna,
                operacion.nueva_semana,
                len(operacion.filas),
                len(operacion.filas - anteriores),
                len(anteriores - operacion.filas),
            )
        # Verifica el plan antes de persistir archivos auxiliares.
        resultado = self._registro.ejecutar(plan)
        if not simulacion:
            self._almacen_catalogo.guardar(catalogo)
            persistida = self._coincidencias.ejecutar(
                catalogo=catalogo,
                semana=semana,
                simulacion=False,
            )
            if persistida != resolucion:
                raise ConcurrenciaError(
                    "Las decisiones cambiaron antes de registrar. Vuelva a resolver."
                )
            resultado = self._registro.ejecutar(plan, simulacion=False)
        return ResultadoAutomatizacion(
            simulacion=simulacion,
            resolucion=resolucion,
            registro=resultado,
        )
