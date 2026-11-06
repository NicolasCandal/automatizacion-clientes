"""Construcción pura de personas y ubicaciones con identidades estables."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from uuid import NAMESPACE_URL, uuid5

from .errores import ValidacionError
from .modelos import (
    AparicionCliente,
    CatalogoClientes,
    FilaCliente,
    HojaInfo,
    PersonaCliente,
    UbicacionCliente,
)
from .normalizacion import extraer_titulares, generar_claves


@dataclass(slots=True)
class _PersonaEnConstruccion:
    nombre_canonico: str
    titulares: list[str] = field(default_factory=list)
    apariciones: dict[UbicacionCliente, AparicionCliente] = field(default_factory=dict)


def construir_catalogo(
    *,
    identificador_origen: str,
    revision: str,
    generado_en: datetime,
    hojas: tuple[HojaInfo, ...],
    clientes_por_hoja: Mapping[str, tuple[FilaCliente, ...]],
    anterior: CatalogoClientes | None = None,
) -> CatalogoClientes:
    """Agrupa titulares normalizados sin alterar filas, celdas ni IDs anteriores.

    Usa las mismas reglas de separación y claves que el índice histórico.
    Conserva el primer titular como nombre canónico y todas las variantes.
    La igualdad de claves sigue siendo una heurística; no distingue homónimos.

    Args:
        identificador_origen: Identidad estable de la planilla que origina el catálogo.
        revision: Revisión opaca verificada por el servicio.
        generado_en: Instante con zona horaria entregado por el servicio.
        hojas: Estructura completa obtenida del origen, en su orden original.
        clientes_por_hoja: Todas las filas leídas, incluidas las no contiguas.
        anterior: Catálogo válido del mismo origen, para conservar IDs por clave.

    Returns:
        Instantánea inmutable, con personas ordenadas por clave de comparación.

    Raises:
        ValidacionError: Estructura inconsistente, filas repetidas o fuera de
            límites, nombres sin titulares normalizables o colisión de identidades.
    """
    base = CatalogoClientes(
        identificador_origen=identificador_origen,
        revision=revision,
        generado_en=generado_en,
        hojas=hojas,
        personas=(),
    )
    if set(clientes_por_hoja) != {hoja.nombre for hoja in hojas}:
        raise ValidacionError("Las filas del catálogo deben corresponder a todas sus hojas.")
    if anterior is not None and anterior.identificador_origen != identificador_origen:
        raise ValidacionError("El catálogo anterior debe pertenecer al mismo origen.")
    acumuladas: dict[str, _PersonaEnConstruccion] = {}
    for hoja in hojas:
        filas_vistas: set[int] = set()
        for cliente in clientes_por_hoja[hoja.nombre]:
            if cliente.fila in filas_vistas or cliente.fila > hoja.ultima_fila_con_cliente:
                raise ValidacionError(
                    "Las filas leídas no pueden repetirse ni superar el límite de la hoja."
                )
            filas_vistas.add(cliente.fila)
            titulares = extraer_titulares(cliente.nombre_crudo)
            if not titulares:
                raise ValidacionError("Una celda de cliente no contiene titulares utilizables.")
            aparicion = AparicionCliente(
                ubicacion=UbicacionCliente(hoja=hoja.nombre, fila=cliente.fila),
                nombre_crudo=cliente.nombre_crudo,
            )
            for titular in titulares:
                clave = generar_claves(titular).clave_orden
                if not clave:
                    raise ValidacionError("Un titular no contiene texto normalizable.")
                persona = acumuladas.setdefault(
                    clave, _PersonaEnConstruccion(nombre_canonico=titular)
                )
                if titular not in persona.titulares:
                    persona.titulares.append(titular)
                persona.apariciones[aparicion.ubicacion] = aparicion

    anteriores = (
        {} if anterior is None else {persona.clave_orden: persona for persona in anterior.personas}
    )
    reservados = {persona.persona_id: clave for clave, persona in anteriores.items()}
    espacio_origen = uuid5(
        NAMESPACE_URL, "automatizacion-clientes/catalogo/v1/" + identificador_origen
    )
    personas: list[PersonaCliente] = []
    for clave, acumulada in sorted(acumuladas.items()):
        persona_id = (
            anteriores[clave].persona_id
            if clave in anteriores
            else "p_" + uuid5(espacio_origen, clave).hex
        )
        if persona_id in reservados and reservados[persona_id] != clave:
            raise ValidacionError("Una identidad del catálogo está reservada para otra persona.")
        reservados[persona_id] = clave
        personas.append(
            PersonaCliente(
                persona_id=persona_id,
                clave_orden=clave,
                nombre_canonico=acumulada.nombre_canonico,
                titulares=tuple(acumulada.titulares),
                apariciones=tuple(acumulada.apariciones.values()),
            )
        )
    return CatalogoClientes(
        identificador_origen=base.identificador_origen,
        revision=base.revision,
        generado_en=base.generado_en,
        hojas=base.hojas,
        personas=tuple(personas),
    )
