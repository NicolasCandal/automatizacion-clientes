"""Conversión validada de diccionarios históricos a los modelos actuales.

El catálogo reconstruido aquí es solo un índice de coincidencias en memoria.
No constituye una caché de origen ni evidencia para registrar marcas.
"""

from datetime import UTC, datetime
from typing import NotRequired, Required, TypedDict, cast

from automatizacion_clientes.core.errores import EstructuraError
from automatizacion_clientes.core.modelos import (
    AliasContacto,
    AparicionCliente,
    CatalogoClientes,
    ContactoIgnorado,
    ContactoPendiente,
    ContactoResuelto,
    ContactoSemanal,
    DestinoAlias,
    HojaInfo,
    PersonaCliente,
    ResultadoContacto,
    UbicacionCliente,
)


class AparicionHistorica(TypedDict):
    """Ubicación y nombre original del formato anterior."""

    hoja: str
    fila: int
    nombre_crudo: str


class PersonaHistorica(TypedDict):
    """Persona con identidad conservada durante la conversión."""

    id: str
    clave_orden: str
    nombre_canonico: str
    titulares: list[str]
    apariciones: list[AparicionHistorica]


class HojaHistorica(TypedDict):
    """Estructura anterior más la última fila necesaria para detectar huecos."""

    sheet_id: int
    col_nombre: int
    primera_col_semana: int | None
    ultima_col_semana: int | None
    ultimo_encabezado_semana: str | None
    filas_cliente: int
    ultima_fila_con_cliente: int


class CatalogoHistorico(TypedDict, total=False):
    """Vista histórica de catálogo; no se persiste como el esquema actual."""

    personas: Required[list[PersonaHistorica]]
    spreadsheet_id: str
    revision: str
    generado_en: str
    hojas: dict[str, HojaHistorica]


class DestinoHistorico(TypedDict):
    """Persona y hojas elegidas por un alias histórico."""

    persona_id: str
    hojas: list[str]


class AliasHistorico(TypedDict):
    """Vista histórica de una asociación tipada."""

    personas: list[DestinoHistorico]
    telefono: str | None


class ContactoHistorico(TypedDict):
    """Metadatos de entrada, nunca contenido de mensajes."""

    nombre_crudo: str
    ultima_fecha: str
    telefono: NotRequired[str | None]
    es_grupo: NotRequired[bool]


class CandidatoHistorico(TypedDict):
    """Candidato resumido en la interfaz anterior."""

    persona_id: str
    nombre: str
    score: float


class ResultadoHistorico(TypedDict, total=False):
    """Vista de resultado; ubicaciones conserva todas las filas de una hoja."""

    tipo: str
    nombre_crudo: str
    persona_id: str
    hojas: list[str]
    fila_por_hoja: dict[str, int]
    ubicaciones: list[dict[str, str | int]]
    via: str
    score: float
    motivo: str
    candidatos: list[CandidatoHistorico]
    avisos: list[str]


class ResolucionHistorica(TypedDict):
    """Colecciones del resultado anterior."""

    resueltos: list[ResultadoHistorico]
    pendientes: list[ResultadoHistorico]
    ignorados: list[ResultadoHistorico]
    avisos: list[str]


def objeto(valor: object) -> dict[str, object]:
    """Exige un objeto de claves de texto sin ocultar datos inválidos."""
    if not isinstance(valor, dict) or any(not isinstance(clave, str) for clave in valor):
        raise EstructuraError("El formato anterior requiere un objeto de claves de texto.")
    return cast(dict[str, object], valor)


def lista(valor: object) -> list[object]:
    """Exige una colección JSON antes de convertirla."""
    if not isinstance(valor, list):
        raise EstructuraError("El formato anterior requiere una lista.")
    return cast(list[object], valor)


def texto(valor: object) -> str:
    """Exige texto sin inferir identidades."""
    if not isinstance(valor, str):
        raise EstructuraError("El formato anterior requiere texto.")
    return valor


def entero(valor: object) -> int:
    """Exige índices enteros y rechaza booleanos."""
    if type(valor) is not int:
        raise EstructuraError("El formato anterior requiere un índice entero.")
    return valor


def catalogo_para_coincidencias(entrada: object) -> CatalogoClientes:
    """Conserva identidades y apariciones; construye solo un índice en memoria."""
    documento = objeto(entrada)
    personas: list[PersonaCliente] = []
    ultimas: dict[str, int] = {}
    for valor in lista(documento.get("personas")):
        persona = objeto(valor)
        apariciones: list[AparicionCliente] = []
        for valor_aparicion in lista(persona.get("apariciones")):
            aparicion = objeto(valor_aparicion)
            nombre = texto(aparicion.get("hoja"))
            fila = entero(aparicion.get("fila"))
            ultimas[nombre] = max(fila, ultimas.get(nombre, 0))
            apariciones.append(
                AparicionCliente(
                    ubicacion=UbicacionCliente(hoja=nombre, fila=fila),
                    nombre_crudo=texto(aparicion.get("nombre_crudo")),
                )
            )
        personas.append(
            PersonaCliente(
                persona_id=texto(persona.get("id")),
                clave_orden=texto(persona.get("clave_orden")),
                nombre_canonico=texto(persona.get("nombre_canonico")),
                titulares=tuple(texto(titular) for titular in lista(persona.get("titulares"))),
                apariciones=tuple(apariciones),
            )
        )
    return CatalogoClientes(
        identificador_origen="compatibilidad:solo-coincidencias",
        revision="sin-revision-de-origen",
        generado_en=datetime(1970, 1, 1, tzinfo=UTC),
        hojas=tuple(
            HojaInfo(
                nombre=nombre,
                sheet_id=indice,
                col_nombre=1,
                primera_col_semana=None,
                ultima_col_semana=None,
                ultima_fila_con_cliente=ultima,
            )
            for indice, (nombre, ultima) in enumerate(sorted(ultimas.items()))
        ),
        personas=tuple(personas),
    )


def contacto_desde_historico(entrada: object) -> ContactoSemanal:
    """Valida metadatos del contacto sin asumir fechas ausentes."""
    from datetime import date

    contacto = objeto(entrada)
    fecha_texto = texto(contacto.get("ultima_fecha"))
    try:
        fecha = date.fromisoformat(fecha_texto)
    except ValueError as causa:
        raise EstructuraError("La fecha del contacto no es válida.") from causa
    if fecha.isoformat() != fecha_texto:
        raise EstructuraError("La fecha del contacto requiere YYYY-MM-DD.")
    grupo = contacto.get("es_grupo", False)
    if type(grupo) is not bool:
        raise EstructuraError("El indicador de grupo debe ser booleano.")
    telefono = contacto.get("telefono")
    return ContactoSemanal(
        nombre_crudo=texto(contacto.get("nombre_crudo")),
        ultima_fecha=fecha,
        telefono=None if telefono is None else texto(telefono),
        es_grupo=grupo,
    )


def alias_desde_historico(nombre: str, entrada: object) -> AliasContacto:
    """Convierte destinos y teléfono aplicando las invariantes actuales."""
    documento = objeto(entrada)
    destinos: list[DestinoAlias] = []
    for valor in lista(documento.get("personas")):
        destino = objeto(valor)
        hojas = tuple(texto(hoja) for hoja in lista(destino.get("hojas")))
        if len(set(hojas)) != len(hojas):
            raise EstructuraError("Las hojas de un alias no pueden estar duplicadas.")
        destinos.append(
            DestinoAlias(
                persona_id=texto(destino.get("persona_id")),
                hojas=frozenset(hojas),
            )
        )
    telefono = documento.get("telefono")
    return AliasContacto(
        nombre_crudo=nombre,
        personas=tuple(destinos),
        telefono=None if telefono is None else texto(telefono),
    )


def alias_a_historico(alias: AliasContacto) -> AliasHistorico:
    """Devuelve una copia de la asociación sin exponer estado mutable del almacén."""
    return {
        "personas": [
            {"persona_id": destino.persona_id, "hojas": sorted(destino.hojas)}
            for destino in alias.personas
        ],
        "telefono": alias.telefono,
    }


def resultado_a_historico(resultado: ResultadoContacto) -> ResultadoHistorico:
    """Adapta la presentación; las decisiones pertenecen exclusivamente al motor."""
    base: ResultadoHistorico = {
        "tipo": resultado.tipo,
        "nombre_crudo": resultado.contacto.nombre_crudo,
    }
    if isinstance(resultado, ContactoResuelto):
        primeras: dict[str, int] = {}
        for ubicacion in resultado.ubicaciones:
            primeras.setdefault(ubicacion.hoja, ubicacion.fila)
        base["persona_id"] = resultado.persona_id
        base["hojas"] = sorted(primeras)
        base["fila_por_hoja"] = primeras
        base["ubicaciones"] = [
            {"hoja": ubicacion.hoja, "fila": ubicacion.fila} for ubicacion in resultado.ubicaciones
        ]
        base["via"] = resultado.via.value
        base["score"] = resultado.puntuacion
    elif isinstance(resultado, ContactoPendiente):
        base["motivo"] = resultado.motivo.value
        base["candidatos"] = [
            {
                "persona_id": candidato.persona_id,
                "nombre": candidato.nombre_canonico,
                "score": candidato.puntuacion,
            }
            for candidato in resultado.candidatos
        ]
    elif isinstance(resultado, ContactoIgnorado):
        base["motivo"] = resultado.motivo.value
    return base
