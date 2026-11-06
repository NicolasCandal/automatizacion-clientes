"""Planificación, simulación y registro semanal mediante puertos independientes."""

import logging
from collections.abc import Mapping
from dataclasses import replace

from automatizacion_clientes.core.contratos import EscritorClientes, LectorRegistroClientes
from automatizacion_clientes.core.errores import (
    ConcurrenciaError,
    ConfiguracionError,
    RegistroInterrumpidoError,
)
from automatizacion_clientes.core.modelos import (
    CatalogoClientes,
    ResolucionSemanal,
    SemanaContactos,
)
from automatizacion_clientes.core.registro import (
    Encabezados,
    PlanRegistro,
    ResultadoRegistro,
    clientes_del_catalogo,
    construir_operaciones,
)

LOGGER = logging.getLogger(__name__)


class ServicioRegistro:
    """Planifica con lecturas y aplica con respaldo y verificaciones entre hojas.

    Requiere que lector y escritor apunten al mismo origen y un único escritor.
    Nunca reintenta el registro completo ni restaura automáticamente un respaldo:
    una escritura fallida puede haber tenido efecto. No persiste informes,
    catálogos o aliases ni cierra los adaptadores que recibe.
    """

    def __init__(
        self,
        *,
        lector_clientes: LectorRegistroClientes,
        escritor_clientes: EscritorClientes,
        identificador_origen: str,
        primeras_columnas: Mapping[str, int] | None = None,
    ) -> None:
        """Recibe dependencias sin hacer lecturas, respaldos ni escrituras.

        Args:
            lector_clientes: Origen con todas sus columnas semanales consultables.
            escritor_clientes: Destino del mismo origen, con verificación de revisión.
            identificador_origen: La misma identidad estable que usa el catálogo.
            primeras_columnas: Tabla completa opcional; por defecto C, o D en Balanz.
        """
        if not identificador_origen.strip():
            raise ConfiguracionError("Se necesita un identificador estable del origen.")
        self._lector = lector_clientes
        self._escritor = escritor_clientes
        self._origen = identificador_origen
        self._primeras = (
            dict(primeras_columnas)
            if primeras_columnas is not None
            else {
                "Inviu": 3,
                "IOL": 3,
                "Bull": 3,
                "Balanz": 4,
                "PPI": 3,
            }
        )

    def planificar(
        self,
        *,
        catalogo: CatalogoClientes,
        semana: SemanaContactos,
        resolucion: ResolucionSemanal,
        encabezado: str,
        permitir_pendientes: bool = False,
    ) -> PlanRegistro:
        """Valida evidencias y consulta el origen sin efectuar ninguna escritura.

        Args:
            catalogo: Instantánea del mismo origen y revisión actual.
            semana: Contactos originales y período explícito de la resolución.
            resolucion: Resultado completo calculado sobre ese catálogo y semana.
            encabezado: Identificador de semana; el llamador lo vincula al período.
            permitir_pendientes: Admite reemplazar usando solo los resueltos.

        Returns:
            Una operación por hoja, con marcas deseadas y precondiciones actuales.

        Raises:
            ConfiguracionError: El catálogo pertenece a otro origen.
            ConcurrenciaError: El catálogo está obsoleto o el origen cambia al leer.
            ValidacionError: Evidencias incompletas o destinos inconsistentes.
            InfraestructuraError: Falló la lectura del origen.
        """
        if catalogo.identificador_origen != self._origen:
            raise ConfiguracionError("El catálogo pertenece a otro origen.")
        self._comprobar_revision(catalogo.revision)
        encabezados = {
            hoja.nombre: self._lector.leer_encabezados(hoja.nombre) for hoja in catalogo.hojas
        }
        operaciones = construir_operaciones(
            catalogo=catalogo,
            semana=semana,
            resolucion=resolucion,
            encabezado=encabezado,
            encabezados=encabezados,
            primeras_columnas=self._primeras,
            permitir_pendientes=permitir_pendientes,
        )
        marcas = tuple(
            frozenset()
            if operacion.nueva_semana
            else self._lector.leer_marcas(operacion.hoja, operacion.columna)
            for operacion in operaciones
        )
        plan = PlanRegistro(
            catalogo=catalogo,
            semana=semana,
            resolucion=resolucion,
            encabezado=encabezado,
            permitir_pendientes=permitir_pendientes,
            operaciones=operaciones,
            encabezados_anteriores=tuple(encabezados[operacion.hoja] for operacion in operaciones),
            marcas_anteriores=marcas,
        )
        revision = self._verificar_estado(plan, ())
        if revision != catalogo.revision:
            raise ConcurrenciaError("El origen cambió durante la planificación del registro.")
        LOGGER.info("Registro planificado: %s hojas.", len(operaciones))
        return plan

    def ejecutar(self, plan: PlanRegistro, *, simulacion: bool = True) -> ResultadoRegistro:
        """Revalida el plan y simula por defecto; aplica solo las hojas con cambios.

        Un registro real crea un respaldo antes de la primera escritura. Cada
        escritura usa la revisión verificada tras la anterior y comprueba su
        resultado, clientes y encabezados. Si no hay cambios, no crea respaldo.

        Args:
            plan: Propuesta devuelta por ``planificar``; no se adapta un plan viejo.
            simulacion: Evita tanto el respaldo como la aplicación de marcas.

        Returns:
            Plan, respaldo si existe, hojas verificadas y última revisión.

        Raises:
            ConcurrenciaError: El plan ya no corresponde al origen antes de respaldar.
            RegistroInterrumpidoError: Falló una fase posterior al respaldo; conserva
                su ruta, hojas confirmadas, hoja en curso y causa original.
            AutomatizacionError: Fallos de validación, lectura o creación del respaldo.
        """
        vigente = self.planificar(
            catalogo=plan.catalogo,
            semana=plan.semana,
            resolucion=plan.resolucion,
            encabezado=plan.encabezado,
            permitir_pendientes=plan.permitir_pendientes,
        )
        if vigente != plan:
            raise ConcurrenciaError("El plan o sus precondiciones cambiaron. Vuelva a planificar.")
        cambios = tuple(
            operacion
            for operacion, anteriores in zip(plan.operaciones, plan.marcas_anteriores, strict=True)
            if operacion.nueva_semana or operacion.filas != anteriores
        )
        if simulacion or not cambios:
            LOGGER.info(
                "Registro sin escrituras: simulación=%s; %s hojas con cambios.",
                simulacion,
                len(cambios),
            )
            return ResultadoRegistro(
                plan=plan,
                simulacion=simulacion,
                respaldo=None,
                hojas_aplicadas=(),
                revision_final=plan.catalogo.revision,
            )
        respaldo = self._escritor.crear_respaldo()
        confirmadas: list[str] = []
        en_curso: str | None = None
        try:
            revision = self._verificar_estado(plan, ())
            if revision != plan.catalogo.revision:
                raise ConcurrenciaError("El origen cambió durante el respaldo.")
            for operacion in cambios:
                en_curso = operacion.hoja
                self._escritor.aplicar_marcas(
                    operacion.hoja,
                    operacion.columna,
                    operacion.encabezado,
                    operacion.filas,
                    revision_esperada=revision,
                )
                revision = self._verificar_estado(plan, (*confirmadas, operacion.hoja))
                confirmadas.append(operacion.hoja)
                en_curso = None
        except Exception as causa:
            LOGGER.error(
                "Registro interrumpido: %s hojas confirmadas; resultado en curso incierto=%s.",
                len(confirmadas),
                en_curso is not None,
            )
            raise RegistroInterrumpidoError(
                respaldo=respaldo,
                hojas_confirmadas=tuple(confirmadas),
                hoja_en_curso=en_curso,
            ) from causa
        LOGGER.info("Registro aplicado y verificado: %s hojas.", len(confirmadas))
        return ResultadoRegistro(
            plan=plan,
            simulacion=False,
            respaldo=respaldo,
            hojas_aplicadas=tuple(confirmadas),
            revision_final=revision,
        )

    def _comprobar_revision(self, esperada: str) -> None:
        if self._lector.revision() != esperada:
            raise ConcurrenciaError("El origen cambió. Actualice el catálogo y vuelva a resolver.")

    def _verificar_estado(self, plan: PlanRegistro, aplicadas: tuple[str, ...]) -> str:
        revision = self._lector.revision()
        esperados = clientes_del_catalogo(plan.catalogo)
        hojas_esperadas = []
        for hoja, operacion, encabezados, marcas in zip(
            plan.catalogo.hojas,
            plan.operaciones,
            plan.encabezados_anteriores,
            plan.marcas_anteriores,
            strict=True,
        ):
            aplicada = operacion.hoja in aplicadas
            actuales: Encabezados = encabezados
            if aplicada and operacion.nueva_semana:
                actuales = (*encabezados, (operacion.columna, operacion.encabezado))
                hoja = replace(
                    hoja,
                    primera_col_semana=hoja.primera_col_semana or operacion.columna,
                    ultima_col_semana=operacion.columna,
                )
            hojas_esperadas.append(hoja)
            if (
                self._lector.leer_encabezados(hoja.nombre) != actuales
                or self._lector.leer_clientes(hoja.nombre) != esperados[hoja.nombre]
            ):
                raise ConcurrenciaError("Los clientes o encabezados no corresponden al plan.")
            if not operacion.nueva_semana or aplicada:
                actuales_marcas = self._lector.leer_marcas(hoja.nombre, operacion.columna)
                if actuales_marcas != (operacion.filas if aplicada else marcas):
                    raise ConcurrenciaError(
                        "Las marcas no corresponden al estado esperado del plan."
                    )
        if self._lector.listar_hojas() != tuple(hojas_esperadas):
            raise ConcurrenciaError("La estructura del origen no corresponde al plan.")
        self._comprobar_revision(revision)
        return revision
