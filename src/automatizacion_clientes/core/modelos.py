"""Modelos inmutables del registro semanal; solo dependen de Python estándar.

Las fechas, los números y las colecciones llegan tipados desde los adaptadores.
Estos modelos validan sus invariantes sin leer archivos, configurar logging ni
decidir coincidencias. La conversión desde y hacia JSON pertenece a ``infra``.
"""

from __future__ import annotations

from collections.abc import Hashable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from math import isfinite
from typing import Literal

from .errores import ValidacionError


class ViaCoincidencia(StrEnum):
    """Origen de la evidencia utilizada para identificar una persona."""

    TELEFONO = "telefono"
    ALIAS = "alias"
    EXACTO = "exacto"
    FUZZY = "fuzzy"


class MotivoPendiente(StrEnum):
    """Razones por las que un contacto requiere revisión humana."""

    AMBIGUO = "ambiguo"
    SIN_MATCH = "sin_match"
    BROKER_NO_COINCIDE = "broker_no_coincide"


class MotivoIgnorado(StrEnum):
    """Razones para excluir un contacto del registro semanal."""

    GRUPO = "grupo"
    EN_LISTA_NEGRA = "en_lista_negra"


def _validar_texto(valor: str, nombre: str) -> None:
    """Exige texto significativo sin modificar el original ni exponer su valor."""
    if not valor.strip():
        raise ValidacionError(f"{nombre} no puede estar vacío.")


def _validar_entero(valor: int, nombre: str, minimo: int = 1) -> None:
    """Rechaza índices inválidos, incluidos los booleanos de Python."""
    if type(valor) is not int or valor < minimo:
        raise ValidacionError(f"{nombre} debe ser un entero mayor o igual que {minimo}.")


def _validar_unicos(valores: Iterable[Hashable], nombre: str) -> None:
    """Detecta identificadores o ubicaciones repetidos sin descartar registros."""
    elementos = tuple(valores)
    if len(elementos) != len(set(elementos)):
        raise ValidacionError(f"{nombre} no puede contener duplicados.")


def _validar_puntuacion(puntuacion: float) -> None:
    """Exige una puntuación finita en la escala inclusiva de cero a cien."""
    if isinstance(puntuacion, bool) or not isfinite(puntuacion) or not 0 <= puntuacion <= 100:
        raise ValidacionError("La puntuación debe ser un número finito entre 0 y 100.")


@dataclass(frozen=True, slots=True, kw_only=True)
class PeriodoSemanal:
    """Intervalo inclusivo de fechas; admite una semana aún en curso.

    No se impone una duración de siete días: los rangos actuales son
    configurables y pueden cubrir únicamente los días hábiles.
    """

    desde: date
    hasta: date

    def __post_init__(self) -> None:
        """Evita mezclar fechas de calendario con instantes o invertir el rango."""
        if type(self.desde) is not date or type(self.hasta) is not date:
            raise ValidacionError("El período debe usar fechas de calendario, sin hora.")
        if self.hasta < self.desde:
            raise ValidacionError("La fecha final no puede ser anterior a la inicial.")

    def contiene(self, fecha: date) -> bool:
        """Indica si una fecha de calendario pertenece al intervalo inclusivo."""
        if type(fecha) is not date:
            raise ValidacionError("La fecha consultada no debe incluir hora.")
        return self.desde <= fecha <= self.hasta


@dataclass(frozen=True, slots=True, kw_only=True)
class HojaInfo:
    """Estructura de una hoja, con filas y columnas numeradas desde uno.

    ``sheet_id`` admite cero, tanto para Sheets como para el índice del Excel.
    Ambas columnas semanales son ``None`` cuando aún no existen semanas.
    ``ultima_fila_con_cliente`` puede señalar la fila anterior al inicio de
    datos cuando la hoja está vacía; el adaptador conoce ese inicio.
    """

    nombre: str
    sheet_id: int
    col_nombre: int
    primera_col_semana: int | None
    ultima_col_semana: int | None
    ultima_fila_con_cliente: int

    def __post_init__(self) -> None:
        """Comprueba que los índices y los límites de columnas sean coherentes."""
        _validar_texto(self.nombre, "El nombre de la hoja")
        _validar_entero(self.sheet_id, "sheet_id", minimo=0)
        _validar_entero(self.col_nombre, "La columna de nombres")
        _validar_entero(self.ultima_fila_con_cliente, "La última fila", minimo=0)
        primera = self.primera_col_semana
        ultima = self.ultima_col_semana
        if (primera is None) != (ultima is None):
            raise ValidacionError(
                "Las columnas semanales deben estar ambas definidas o ambas ausentes."
            )
        if primera is not None and ultima is not None:
            _validar_entero(primera, "La primera columna semanal")
            _validar_entero(ultima, "La última columna semanal")
            if primera <= self.col_nombre or ultima < primera:
                raise ValidacionError(
                    "Las columnas semanales deben seguir a los nombres y estar ordenadas."
                )


@dataclass(frozen=True, slots=True, kw_only=True)
class FilaCliente:
    """Fila leída por un repositorio, conservando el nombre original."""

    fila: int
    nombre_crudo: str = field(repr=False)

    def __post_init__(self) -> None:
        """Rechaza filas inválidas y celdas sin un nombre utilizable."""
        _validar_entero(self.fila, "La fila del cliente")
        _validar_texto(self.nombre_crudo, "El nombre original")


@dataclass(frozen=True, slots=True, kw_only=True)
class UbicacionCliente:
    """Referencia a una fila de cliente dentro de una hoja."""

    hoja: str
    fila: int

    def __post_init__(self) -> None:
        """Valida la referencia sin consultar ninguna planilla."""
        _validar_texto(self.hoja, "La hoja del cliente")
        _validar_entero(self.fila, "La fila del cliente")


@dataclass(frozen=True, slots=True, kw_only=True)
class AparicionCliente:
    """Relación entre una ubicación y el texto original de su celda."""

    ubicacion: UbicacionCliente
    nombre_crudo: str = field(repr=False)

    def __post_init__(self) -> None:
        """Conserva el texto original, incluidos los nombres de cotitulares."""
        _validar_texto(self.nombre_crudo, "El nombre original")


@dataclass(frozen=True, slots=True, kw_only=True)
class PersonaCliente:
    """Persona identificada que puede aparecer en varias filas y hojas.

    Dos personas distintas pueden compartir una ubicación por cotitularidad.
    Una misma persona puede tener varias filas en una misma hoja; se conservan
    todas para que el servicio no marque solamente la primera.
    """

    persona_id: str
    clave_orden: str = field(repr=False)
    nombre_canonico: str = field(repr=False)
    titulares: tuple[str, ...] = field(repr=False)
    apariciones: tuple[AparicionCliente, ...] = field(repr=False)

    def __post_init__(self) -> None:
        """Exige identidad y apariciones consistentes, sin normalizar nombres."""
        _validar_texto(self.persona_id, "El identificador de persona")
        _validar_texto(self.clave_orden, "La clave de orden")
        _validar_texto(self.nombre_canonico, "El nombre canónico")
        if not self.titulares or not self.apariciones:
            raise ValidacionError("La persona debe tener titulares y apariciones.")
        for titular in self.titulares:
            _validar_texto(titular, "El titular")
        _validar_unicos(self.titulares, "Los titulares")
        _validar_unicos(self.ubicaciones, "Las ubicaciones de la persona")

    @property
    def ubicaciones(self) -> tuple[UbicacionCliente, ...]:
        """Devuelve todas las ubicaciones sin perder filas de una misma hoja."""
        return tuple(aparicion.ubicacion for aparicion in self.apariciones)

    @property
    def hojas(self) -> frozenset[str]:
        """Devuelve las hojas donde aparece la persona, sin duplicados."""
        return frozenset(ubicacion.hoja for ubicacion in self.ubicaciones)


@dataclass(frozen=True, slots=True, kw_only=True)
class CatalogoClientes:
    """Instantánea de clientes asociada a una revisión concreta del origen."""

    identificador_origen: str = field(repr=False)
    revision: str
    generado_en: datetime
    hojas: tuple[HojaInfo, ...]
    personas: tuple[PersonaCliente, ...] = field(repr=False)

    def __post_init__(self) -> None:
        """Comprueba identidades, referencias, límites de filas y zona horaria."""
        _validar_texto(self.identificador_origen, "El origen del catálogo")
        _validar_texto(self.revision, "La revisión del catálogo")
        if self.generado_en.utcoffset() is None:
            raise ValidacionError("La fecha de generación del catálogo debe tener zona horaria.")
        _validar_unicos((hoja.nombre for hoja in self.hojas), "Los nombres de hoja")
        _validar_unicos((hoja.sheet_id for hoja in self.hojas), "Los identificadores de hoja")
        _validar_unicos((persona.persona_id for persona in self.personas), "Las personas")
        _validar_unicos((persona.clave_orden for persona in self.personas), "Las claves de orden")
        nombres_hojas = frozenset(hoja.nombre for hoja in self.hojas)
        ultimas_filas = {hoja.nombre: hoja.ultima_fila_con_cliente for hoja in self.hojas}
        for persona in self.personas:
            if not persona.hojas <= nombres_hojas:
                raise ValidacionError(
                    "Una persona hace referencia a una hoja ausente del catálogo."
                )
            if any(
                ubicacion.fila > ultimas_filas[ubicacion.hoja] for ubicacion in persona.ubicaciones
            ):
                raise ValidacionError(
                    "Una ubicación de cliente supera la última fila declarada para su hoja."
                )


@dataclass(frozen=True, slots=True, kw_only=True)
class ContactoSemanal:
    """Metadatos de un chat, sin almacenar el contenido de los mensajes.

    Un chat sin nombre puede identificarse por teléfono. La normalización del
    número corresponde al adaptador; no se presupone país ni formato aquí.
    Nombres y teléfonos se excluyen de la representación de diagnóstico.
    """

    nombre_crudo: str = field(repr=False)
    ultima_fecha: date
    telefono: str | None = field(default=None, repr=False)
    es_grupo: bool = False

    def __post_init__(self) -> None:
        """Exige fecha de calendario y alguna identidad disponible del chat."""
        if type(self.ultima_fecha) is not date:
            raise ValidacionError("La última fecha del contacto no debe incluir hora.")
        if self.telefono is not None:
            _validar_texto(self.telefono, "El teléfono")
        if not self.nombre_crudo.strip() and self.telefono is None:
            raise ValidacionError("El contacto debe tener nombre o teléfono.")


@dataclass(frozen=True, slots=True, kw_only=True)
class SemanaContactos:
    """Contactos extraídos dentro de un período inclusivo determinado."""

    periodo: PeriodoSemanal
    contactos: tuple[ContactoSemanal, ...] = field(repr=False)

    def __post_init__(self) -> None:
        """Impide registrar como semanales chats de fuera del período."""
        if any(not self.periodo.contiene(contacto.ultima_fecha) for contacto in self.contactos):
            raise ValidacionError("Todos los contactos deben pertenecer al período informado.")


@dataclass(frozen=True, slots=True, kw_only=True)
class CandidatoCoincidencia:
    """Persona propuesta por el comparador, aún sin decisión de registro."""

    persona_id: str
    nombre_canonico: str = field(repr=False)
    hojas: frozenset[str]
    puntuacion: float
    via: ViaCoincidencia

    def __post_init__(self) -> None:
        """Valida la identidad, las hojas y la escala de puntuación."""
        _validar_texto(self.persona_id, "El identificador del candidato")
        _validar_texto(self.nombre_canonico, "El nombre del candidato")
        _validar_puntuacion(self.puntuacion)
        if not self.hojas:
            raise ValidacionError("El candidato debe aparecer en alguna hoja.")
        for hoja in self.hojas:
            _validar_texto(hoja, "La hoja del candidato")


@dataclass(frozen=True, slots=True, kw_only=True)
class ContactoResuelto:
    """Asignación a una persona y a todas las filas concretas que se marcarán.

    El motor decide si la evidencia es suficiente. El modelo no incorpora
    umbrales ni ejecuta escrituras y no impide varias filas en una misma hoja.
    """

    contacto: ContactoSemanal = field(repr=False)
    persona_id: str
    ubicaciones: tuple[UbicacionCliente, ...]
    puntuacion: float
    via: ViaCoincidencia
    tipo: Literal["resuelto"] = field(default="resuelto", init=False)

    def __post_init__(self) -> None:
        """Rechaza asignaciones sin destino o con filas repetidas."""
        _validar_texto(self.persona_id, "El identificador de persona")
        _validar_puntuacion(self.puntuacion)
        if not self.ubicaciones:
            raise ValidacionError("El contacto resuelto debe tener alguna ubicación de destino.")
        _validar_unicos(self.ubicaciones, "Las ubicaciones de destino")


@dataclass(frozen=True, slots=True, kw_only=True)
class ContactoPendiente:
    """Contacto que requiere revisión; no contiene destinos para escribir."""

    contacto: ContactoSemanal = field(repr=False)
    motivo: MotivoPendiente
    candidatos: tuple[CandidatoCoincidencia, ...] = field(default=(), repr=False)
    tipo: Literal["pendiente"] = field(default="pendiente", init=False)

    def __post_init__(self) -> None:
        """Evita mostrar una persona repetida como si fueran candidatos distintos."""
        _validar_unicos((candidato.persona_id for candidato in self.candidatos), "Los candidatos")


@dataclass(frozen=True, slots=True, kw_only=True)
class ContactoIgnorado:
    """Contacto excluido explícitamente por una regla de negocio."""

    contacto: ContactoSemanal = field(repr=False)
    motivo: MotivoIgnorado
    tipo: Literal["ignorado"] = field(default="ignorado", init=False)


type ResultadoContacto = ContactoResuelto | ContactoPendiente | ContactoIgnorado


@dataclass(frozen=True, slots=True, kw_only=True)
class ResolucionSemanal:
    """Resultados agrupados; un chat puede resolver a varios cotitulares.

    Las cantidades representan resultados por titular, no chats únicos.
    Los avisos se conservan para el informe y no se escriben en logs aquí.
    """

    resueltos: tuple[ContactoResuelto, ...] = field(default=(), repr=False)
    pendientes: tuple[ContactoPendiente, ...] = field(default=(), repr=False)
    ignorados: tuple[ContactoIgnorado, ...] = field(default=(), repr=False)
    avisos: tuple[str, ...] = field(default=(), repr=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class DestinoAlias:
    """Identidad aprendida y hojas elegidas, sin filas que puedan quedar obsoletas."""

    persona_id: str
    hojas: frozenset[str]

    def __post_init__(self) -> None:
        """Exige una persona y al menos una hoja de destino."""
        _validar_texto(self.persona_id, "El identificador de persona del alias")
        if not self.hojas:
            raise ValidacionError("El destino del alias debe tener alguna hoja.")
        for hoja in self.hojas:
            _validar_texto(hoja, "La hoja del alias")


@dataclass(frozen=True, slots=True, kw_only=True)
class AliasContacto:
    """Resolución aprendida para un contacto, incluidos sus posibles cotitulares."""

    nombre_crudo: str = field(repr=False)
    personas: tuple[DestinoAlias, ...] = field(repr=False)
    telefono: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Valida la identidad del contacto y evita personas repetidas."""
        if self.telefono is not None:
            _validar_texto(self.telefono, "El teléfono del alias")
        if not self.nombre_crudo.strip() and self.telefono is None:
            raise ValidacionError("El alias debe tener nombre o teléfono.")
        if not self.personas:
            raise ValidacionError("El alias debe apuntar a alguna persona.")
        _validar_unicos((persona.persona_id for persona in self.personas), "Las personas del alias")


@dataclass(frozen=True, slots=True, kw_only=True)
class ClavesNombre:
    """Texto original y claves derivadas por las funciones de normalización.

    Admite claves vacías para que el servicio detecte entradas sin nombre.
    ``generar_claves`` construye las claves; este modelo no vuelve a calcularlas.
    Los tres textos se excluyen de la representación de diagnóstico.
    """

    crudo: str = field(repr=False)
    normalizado: str = field(repr=False)
    clave_orden: str = field(repr=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class NombreConBrokers:
    """Resultado del reconocimiento de brokers, sin decidir una coincidencia.

    Admite un nombre vacío y la ausencia de hojas reconocidas. ``ruido``
    conserva tokens que pueden contener datos personales y se excluye de
    ``repr`` junto con el nombre.
    """

    nombre_sin_parentesis: str = field(repr=False)
    hojas: tuple[str, ...]
    ruido: tuple[str, ...] = field(repr=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class PoliticaCoincidencia:
    """Umbrales de decisión y cantidad de sugerencias para revisión humana.

    El límite de sugerencias se aplica después de decidir; nunca recorta los
    candidatos que participan en la detección de ambigüedad.
    """

    umbral_automatico: float = 92.0
    umbral_revision: float = 80.0
    limite_sugerencias: int = 3

    def __post_init__(self) -> None:
        """Valida puntuaciones y orden de los umbrales sin consultar configuración."""
        _validar_puntuacion(self.umbral_automatico)
        _validar_puntuacion(self.umbral_revision)
        _validar_entero(self.limite_sugerencias, "El límite de sugerencias")
        if self.umbral_revision >= self.umbral_automatico:
            raise ValidacionError("El umbral de revisión debe ser menor que el automático.")
