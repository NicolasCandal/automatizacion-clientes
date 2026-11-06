"""Orquestación semanal con simulación sin escrituras y aprendizaje diferido."""

import logging
from collections.abc import Mapping

from automatizacion_clientes.core.aliases import conciliar_alias, proponer_alias
from automatizacion_clientes.core.coincidencia import MotorCoincidencias
from automatizacion_clientes.core.contratos import (
    AlmacenAliases,
    AlmacenCatalogo,
    AlmacenResoluciones,
    ComparadorNombres,
    LectorContactos,
    ListaExclusion,
)
from automatizacion_clientes.core.errores import (
    AutomatizacionError,
    ConcurrenciaError,
    ConfiguracionError,
)
from automatizacion_clientes.core.modelos import (
    AliasContacto,
    CatalogoClientes,
    ContactoIgnorado,
    ContactoPendiente,
    ContactoResuelto,
    ContactoSemanal,
    PoliticaCoincidencia,
    ResolucionSemanal,
    ResultadoContacto,
    SemanaContactos,
)

LOGGER = logging.getLogger(__name__)


class ServicioCoincidencias:
    """Conecta las fuentes, el motor puro y la persistencia de sus resultados.

    Resuelve todos los contactos antes de aprender aliases. Solo aprende de
    contactos completamente resueltos por exacto/fuzzy, conservando el criterio
    del comando anterior. Las decisiones por aliases o teléfono no se reaprenden.
    La simulación usa la misma conciliación de aliases que el almacén real.

    El informe se guarda antes que los aliases. No hay transacción entre ambos:
    si falla la persistencia de aliases, el informe queda disponible y se lanza
    el error. En ese caso se deben recrear el servicio y la sesión de aliases.
    No recibe un escritor de planillas ni comprueba la vigencia del catálogo
    contra Excel o Sheets; esa responsabilidad pertenece al flujo de registro.
    """

    def __init__(
        self,
        *,
        lector_contactos: LectorContactos,
        almacen_catalogo: AlmacenCatalogo,
        almacen_aliases: AlmacenAliases,
        lista_exclusion: ListaExclusion,
        almacen_resoluciones: AlmacenResoluciones,
        comparador: ComparadorNombres,
        politica: PoliticaCoincidencia | None = None,
        tabla_brokers: Mapping[str, str] | None = None,
    ) -> None:
        """Recibe dependencias tipadas sin ejecutar lecturas ni escrituras.

        Args:
            lector_contactos: Fuente del período y los contactos validados.
            almacen_catalogo: Instantánea de personas y ubicaciones del origen.
            almacen_aliases: Sesión de aliases exclusiva de este servicio.
            lista_exclusion: Exclusiones consultables sin modificarlas.
            almacen_resoluciones: Destino del informe completo.
            comparador: Cálculo de similitud sin decisiones de negocio.
            politica: Umbrales puros, normalmente construidos por ``Config``.
            tabla_brokers: Tabla opcional que reemplaza los alias predeterminados.
        """
        self._lector_contactos = lector_contactos
        self._almacen_catalogo = almacen_catalogo
        self._almacen_aliases = almacen_aliases
        self._lista_exclusion = lista_exclusion
        self._almacen_resoluciones = almacen_resoluciones
        self._comparador = comparador
        self._politica = politica
        self._tabla_brokers = None if tabla_brokers is None else dict(tabla_brokers)
        self._sesion_aliases_fallida = False

    def ejecutar(
        self,
        *,
        simulacion: bool = True,
        catalogo: CatalogoClientes | None = None,
        semana: SemanaContactos | None = None,
    ) -> ResolucionSemanal:
        """Resuelve una semana y, si se solicita, persiste informe y aprendizaje.

        La simulación no llama a registrar ni guardar. Los errores de lectura,
        validación o comparación ocurren antes de modificar la sesión de aliases.
        Solo se guardan aliases si hay asociaciones nuevas o completadas.

        Args:
            simulacion: Con ``True`` devuelve los resultados sin efectuar escrituras.
            catalogo: Instantánea opcional del flujo; evita releer la caché.
            semana: Extracción opcional del flujo; evita releer los contactos.

        Returns:
            Todos los resultados y avisos de conciliación, conservando su orden.

        Raises:
            ConfiguracionError: Falta el catálogo o la sesión falló al guardar aliases.
            ConcurrenciaError: Un alias cambió durante la fase de persistencia.
            AutomatizacionError: Fallos de lectura, validación, comparación o guardado.
                Si falla el guardado de aliases, el informe ya estará guardado.
        """
        if self._sesion_aliases_fallida:
            raise ConfiguracionError(
                "Cree otro servicio con una nueva sesión de aliases tras el fallo."
            )
        if semana is None:
            semana = self._lector_contactos.leer()
        if catalogo is None:
            catalogo = self._almacen_catalogo.leer()
        if catalogo is None:
            raise ConfiguracionError(
                "No existe un catálogo. Constrúyalo desde el origen antes de resolver."
            )
        motor = MotorCoincidencias(
            catalogo, self._comparador, politica=self._politica, tabla_brokers=self._tabla_brokers
        )
        aliases_nombre: dict[str, AliasContacto | None] = {}
        aliases_telefono: dict[str, tuple[AliasContacto, ...]] = {}
        resultados: list[ResultadoContacto] = []
        propuestas: list[AliasContacto] = []
        for contacto in semana.contactos:
            decisiones = self._resolver_contacto(motor, contacto, aliases_nombre, aliases_telefono)
            resultados.extend(decisiones)
            propuesta = proponer_alias(contacto, decisiones)
            if propuesta is not None:
                propuestas.append(propuesta)

        plan_aliases: dict[str, AliasContacto] = {}
        avisos: list[str] = []
        for propuesta in propuestas:
            nombre = propuesta.nombre_crudo
            existente = plan_aliases.get(nombre, aliases_nombre[nombre])
            conservado, aviso = conciliar_alias(existente, propuesta)
            if aviso is not None:
                avisos.append(aviso)
            if conservado != existente:
                plan_aliases[nombre] = conservado
        resolucion = ResolucionSemanal(
            resueltos=tuple(valor for valor in resultados if isinstance(valor, ContactoResuelto)),
            pendientes=tuple(valor for valor in resultados if isinstance(valor, ContactoPendiente)),
            ignorados=tuple(valor for valor in resultados if isinstance(valor, ContactoIgnorado)),
            avisos=tuple(avisos),
        )
        if not simulacion:
            self._almacen_resoluciones.guardar(resolucion)
            self._persistir_aliases(plan_aliases)
        LOGGER.info(
            "Coincidencias finalizadas: simulación=%s; "
            "%s resueltos, %s pendientes, %s ignorados y %s avisos.",
            simulacion,
            len(resolucion.resueltos),
            len(resolucion.pendientes),
            len(resolucion.ignorados),
            len(resolucion.avisos),
        )
        return resolucion

    def _resolver_contacto(
        self,
        motor: MotorCoincidencias,
        contacto: ContactoSemanal,
        aliases_nombre: dict[str, AliasContacto | None],
        aliases_telefono: dict[str, tuple[AliasContacto, ...]],
    ) -> tuple[ResultadoContacto, ...]:
        if contacto.es_grupo:
            return motor.resolver_contacto(contacto)
        if self._lista_exclusion.contiene(contacto.nombre_crudo):
            return motor.resolver_contacto(contacto, excluido=True)
        por_telefono: tuple[AliasContacto, ...] = ()
        if contacto.telefono is not None:
            if contacto.telefono not in aliases_telefono:
                aliases_telefono[contacto.telefono] = self._almacen_aliases.buscar_por_telefono(
                    contacto.telefono
                )
            por_telefono = aliases_telefono[contacto.telefono]
        por_nombre = None
        if not por_telefono:
            if contacto.nombre_crudo not in aliases_nombre:
                aliases_nombre[contacto.nombre_crudo] = self._almacen_aliases.buscar_por_nombre(
                    contacto.nombre_crudo
                )
            por_nombre = aliases_nombre[contacto.nombre_crudo]
        return motor.resolver_contacto(
            contacto, aliases_telefono=por_telefono, alias_nombre=por_nombre
        )

    def _persistir_aliases(self, plan: Mapping[str, AliasContacto]) -> None:
        if not plan:
            return
        try:
            for alias in plan.values():
                if self._almacen_aliases.registrar(alias) is not None:
                    raise ConcurrenciaError(
                        "Un alias cambió desde la planificación. Abra una nueva sesión."
                    )
            self._almacen_aliases.guardar()
        except AutomatizacionError:
            self._sesion_aliases_fallida = True
            LOGGER.warning(
                "El informe se guardó, pero falló la persistencia de aliases. Abra otra sesión."
            )
            raise
