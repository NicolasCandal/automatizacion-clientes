"""Vista histórica de catálogo construida por el dominio actual."""

from automatizacion_clientes.core.catalogo import construir_catalogo as construir_catalogo_actual
from automatizacion_clientes.core.errores import ConcurrenciaError
from automatizacion_clientes.infra.formatos_compatibilidad import (
    CatalogoHistorico,
    HojaHistorica,
    PersonaHistorica,
    catalogo_para_coincidencias,
)
from automatizacion_clientes.infra.reloj_sistema import RelojSistema
from automatizacion_clientes.infra.repositorio_compatibilidad import RepositorioCompatible


def construir_catalogo(repo: RepositorioCompatible, identificador_origen: str) -> CatalogoHistorico:
    """Construye con lectura coherente e IDs estables; no guarda una caché antigua."""
    revision = repo.revision()
    hojas = repo.lector.listar_hojas()
    clientes = {hoja.nombre: repo.lector.leer_clientes(hoja.nombre) for hoja in hojas}
    encabezados = {hoja.nombre: repo.lector.ultima_semana(hoja.nombre)[1] for hoja in hojas}
    if repo.revision() != revision:
        raise ConcurrenciaError("El origen cambió durante la construcción del catálogo.")
    catalogo = construir_catalogo_actual(
        identificador_origen=identificador_origen,
        revision=revision,
        generado_en=RelojSistema().ahora(),
        hojas=hojas,
        clientes_por_hoja=clientes,
    )
    informacion: dict[str, HojaHistorica] = {
        hoja.nombre: {
            "sheet_id": hoja.sheet_id,
            "col_nombre": hoja.col_nombre,
            "primera_col_semana": hoja.primera_col_semana,
            "ultima_col_semana": hoja.ultima_col_semana,
            "ultimo_encabezado_semana": encabezados[hoja.nombre] or None,
            "filas_cliente": len(clientes[hoja.nombre]),
            "ultima_fila_con_cliente": hoja.ultima_fila_con_cliente,
        }
        for hoja in hojas
    }
    personas: list[PersonaHistorica] = [
        {
            "id": persona.persona_id,
            "clave_orden": persona.clave_orden,
            "nombre_canonico": persona.nombre_canonico,
            "titulares": list(persona.titulares),
            "apariciones": [
                {
                    "hoja": aparicion.ubicacion.hoja,
                    "fila": aparicion.ubicacion.fila,
                    "nombre_crudo": aparicion.nombre_crudo,
                }
                for aparicion in persona.apariciones
            ],
        }
        for persona in catalogo.personas
    ]
    return {
        "spreadsheet_id": catalogo.identificador_origen,
        "revision": catalogo.revision,
        "generado_en": catalogo.generado_en.isoformat(),
        "hojas": informacion,
        "personas": personas,
    }


def contar_personas_multi_hoja(catalogo: object) -> int:
    """Cuenta mediante las ubicaciones validadas del dominio actual."""
    return sum(len(persona.hojas) > 1 for persona in catalogo_para_coincidencias(catalogo).personas)
