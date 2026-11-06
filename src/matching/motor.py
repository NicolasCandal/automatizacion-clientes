"""Fachada histórica: todas las decisiones las toma MotorCoincidencias."""

from collections.abc import Sequence
from dataclasses import dataclass, field

from automatizacion_clientes.core.aliases import proponer_alias
from automatizacion_clientes.core.coincidencia import MotorCoincidencias
from automatizacion_clientes.infra.comparador_rapidfuzz import ComparadorRapidFuzz
from automatizacion_clientes.infra.formatos_compatibilidad import (
    ResolucionHistorica,
    ResultadoHistorico,
    catalogo_para_coincidencias,
    contacto_desde_historico,
    resultado_a_historico,
)

from .alias_store import AliasStore
from .lista_negra import ListaNegra


@dataclass(frozen=True, slots=True)
class IndiceCoincidencias:
    """Índice opaco de compatibilidad; no representa una revisión del origen."""

    motor: MotorCoincidencias = field(repr=False)


def construir_indice(catalogo: object) -> IndiceCoincidencias:
    """Valida personas y apariciones antes de construir el motor actual."""
    return IndiceCoincidencias(
        MotorCoincidencias(catalogo_para_coincidencias(catalogo), ComparadorRapidFuzz())
    )


def resolver_contacto(
    contacto: object, idx: IndiceCoincidencias, alias_store: AliasStore, lista_negra: ListaNegra
) -> list[ResultadoHistorico]:
    """Adapta datos históricos y registra únicamente aprendizaje completamente resuelto.

    No guarda archivos. Los aliases por teléfono se entregan completos al motor,
    por lo que no se elige silenciosamente la primera asociación de un número.
    """
    identidad = contacto_desde_historico(contacto)
    decisiones = idx.motor.resolver_contacto(
        identidad,
        aliases_telefono=()
        if identidad.telefono is None
        else alias_store.almacen.buscar_por_telefono(identidad.telefono),
        alias_nombre=alias_store.almacen.buscar_por_nombre(identidad.nombre_crudo),
        excluido=lista_negra.contiene(identidad.nombre_crudo),
    )
    propuesta = proponer_alias(identidad, decisiones)
    aviso = None if propuesta is None else alias_store.almacen.registrar(propuesta)
    resultados = [resultado_a_historico(decision) for decision in decisiones]
    if aviso is not None:
        for resultado in resultados:
            resultado["avisos"] = [aviso]
    return resultados


def resolver_semana(
    contactos: Sequence[object], catalogo: object, alias_store: AliasStore, lista_negra: ListaNegra
) -> ResolucionHistorica:
    """Valida todas las decisiones antes de aprender o guardar asociaciones.

    Los llamadores nuevos deben usar ServicioCoincidencias, que además incorpora
    simulación e informe persistido. Esta función conserva el guardado explícito
    de aliases del contrato anterior, después de resolver el lote completo.
    """
    indice = construir_indice(catalogo)
    identidades = tuple(contacto_desde_historico(contacto) for contacto in contactos)
    decisiones = tuple(
        indice.motor.resolver_contacto(
            contacto,
            aliases_telefono=()
            if contacto.telefono is None
            else alias_store.almacen.buscar_por_telefono(contacto.telefono),
            alias_nombre=alias_store.almacen.buscar_por_nombre(contacto.nombre_crudo),
            excluido=lista_negra.contiene(contacto.nombre_crudo),
        )
        for contacto in identidades
    )
    propuestas = [
        propuesta
        for contacto, resultados in zip(identidades, decisiones, strict=True)
        if (propuesta := proponer_alias(contacto, resultados)) is not None
    ]
    avisos = [
        aviso
        for propuesta in propuestas
        if (aviso := alias_store.almacen.registrar(propuesta)) is not None
    ]
    resultado: ResolucionHistorica = {
        "resueltos": [],
        "pendientes": [],
        "ignorados": [],
        "avisos": avisos,
    }
    for resultados in decisiones:
        for decision in resultados:
            adaptado = resultado_a_historico(decision)
            adaptado.pop("tipo", None)
            if decision.tipo == "resuelto":
                resultado["resueltos"].append(adaptado)
            elif decision.tipo == "pendiente":
                resultado["pendientes"].append(adaptado)
            else:
                resultado["ignorados"].append(adaptado)
    if propuestas:
        alias_store.guardar()
    return resultado
