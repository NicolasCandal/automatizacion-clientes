"""Conciliación pura de identidades aprendidas, sin archivos ni logging."""

from dataclasses import replace

from .errores import ValidacionError
from .modelos import (
    AliasContacto,
    ContactoResuelto,
    ContactoSemanal,
    DestinoAlias,
    ResultadoContacto,
    ViaCoincidencia,
)


def conciliar_alias(
    existente: AliasContacto | None, nuevo: AliasContacto
) -> tuple[AliasContacto, str | None]:
    """Devuelve la asociación conservada y un aviso si hay conflicto.

    Compara destinos sin considerar su orden y permite completar un teléfono
    ausente. Nunca cambia destinos ni teléfonos conocidos. No modifica entradas.

    Args:
        existente: Alias previo del mismo nombre original, si existe.
        nuevo: Resolución que se desea incorporar.

    Returns:
        Alias resultante y aviso sin datos personales, o ``None`` si es compatible.

    Raises:
        ValidacionError: Los aliases corresponden a nombres originales distintos.
    """
    if existente is None:
        return nuevo, None
    if existente.nombre_crudo != nuevo.nombre_crudo:
        raise ValidacionError("Solo se pueden conciliar aliases del mismo nombre original.")
    if frozenset(existente.personas) != frozenset(nuevo.personas):
        return existente, "El alias ya tiene otros destinos. Se conservó la asociación anterior."
    if (
        existente.telefono is not None
        and nuevo.telefono is not None
        and existente.telefono != nuevo.telefono
    ):
        return existente, "El alias ya tiene otro teléfono. Se conservó la asociación anterior."
    if existente.telefono is None and nuevo.telefono is not None:
        return replace(existente, telefono=nuevo.telefono), None
    return existente, None


def proponer_alias(
    contacto: ContactoSemanal, resultados: tuple[ResultadoContacto, ...]
) -> AliasContacto | None:
    """Propone aprendizaje para contactos completamente resueltos por nombres.

    Conserva todas las personas y hojas, sin persistir números de fila.

    Args:
        contacto: Identidad original del chat que produjo los resultados.
        resultados: Todas las decisiones del motor para ese contacto.

    Returns:
        Propuesta inmutable, o ``None`` para decisiones parciales o aliases previos.

    Raises:
        ValidacionError: Algún resultado corresponde a otro contacto.
    """
    if not resultados:
        return None
    if any(resultado.contacto != contacto for resultado in resultados):
        raise ValidacionError("Los resultados del aprendizaje deben pertenecer al mismo contacto.")
    destinos: list[DestinoAlias] = []
    for resultado in resultados:
        if not isinstance(resultado, ContactoResuelto) or resultado.via not in {
            ViaCoincidencia.EXACTO,
            ViaCoincidencia.FUZZY,
        }:
            return None
        destinos.append(
            DestinoAlias(
                persona_id=resultado.persona_id,
                hojas=frozenset(ubicacion.hoja for ubicacion in resultado.ubicaciones),
            )
        )
    return AliasContacto(
        nombre_crudo=contacto.nombre_crudo, telefono=contacto.telefono, personas=tuple(destinos)
    )
