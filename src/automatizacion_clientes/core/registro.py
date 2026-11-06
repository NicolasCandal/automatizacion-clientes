"""Plan de marcas y validación de sus evidencias, sin dependencias externas."""

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from automatizacion_clientes.core.errores import ValidacionError
from automatizacion_clientes.core.modelos import (
    CatalogoClientes,
    FilaCliente,
    ResolucionSemanal,
    ResultadoContacto,
    SemanaContactos,
)

type Encabezados = tuple[tuple[int, str], ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class MarcasHoja:
    """Estado completo deseado para la semana de una hoja, incluidos los desmarcados."""

    hoja: str
    columna: int
    encabezado: str
    filas: frozenset[int]
    nueva_semana: bool

    def __post_init__(self) -> None:
        """Exige índices y textos explícitos, sin coerciones de tipos."""
        if not self.hoja.strip() or not self.encabezado.strip():
            raise ValidacionError("La operación requiere hoja y encabezado.")
        if type(self.columna) is not int or self.columna < 1:
            raise ValidacionError("La columna de registro debe ser un entero positivo.")
        if any(type(fila) is not int or fila < 1 for fila in self.filas):
            raise ValidacionError("Las filas de registro deben ser enteros positivos.")


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanRegistro:
    """Propuesta inmutable ligada a las entradas y a una revisión del catálogo.

    Guarda los encabezados y marcas previos para detectar cambios al aplicar.
    Los datos personales se excluyen de ``repr``; no es un informe serializable.
    """

    catalogo: CatalogoClientes = field(repr=False)
    semana: SemanaContactos = field(repr=False)
    resolucion: ResolucionSemanal = field(repr=False)
    encabezado: str
    permitir_pendientes: bool
    operaciones: tuple[MarcasHoja, ...]
    encabezados_anteriores: tuple[Encabezados, ...] = field(repr=False)
    marcas_anteriores: tuple[frozenset[int], ...] = field(repr=False)

    def __post_init__(self) -> None:
        """Mantiene alineadas las operaciones, hojas y precondiciones."""
        nombres = tuple(hoja.nombre for hoja in self.catalogo.hojas)
        if not nombres or tuple(operacion.hoja for operacion in self.operaciones) != nombres:
            raise ValidacionError("El plan debe contener una operación por hoja del catálogo.")
        if not len(nombres) == len(self.encabezados_anteriores) == len(self.marcas_anteriores):
            raise ValidacionError("Las precondiciones del plan deben cubrir todas las hojas.")


@dataclass(frozen=True, slots=True, kw_only=True)
class ResultadoRegistro:
    """Resultado verificable; la simulación nunca tiene respaldo ni hojas aplicadas."""

    plan: PlanRegistro = field(repr=False)
    simulacion: bool
    respaldo: Path | str | None = field(repr=False)
    hojas_aplicadas: tuple[str, ...]
    revision_final: str

    def __post_init__(self) -> None:
        """Impide atribuir efectos a una simulación o escrituras sin respaldo."""
        if not self.revision_final.strip():
            raise ValidacionError("El resultado requiere una revisión final.")
        if self.simulacion and (self.respaldo is not None or self.hojas_aplicadas):
            raise ValidacionError("Una simulación no puede tener efectos de escritura.")
        if self.hojas_aplicadas and self.respaldo is None:
            raise ValidacionError("Las escrituras requieren un respaldo previo.")
        if len(set(self.hojas_aplicadas)) != len(self.hojas_aplicadas) or not set(
            self.hojas_aplicadas
        ) <= {operacion.hoja for operacion in self.plan.operaciones}:
            raise ValidacionError("Las hojas aplicadas deben pertenecer al plan y ser únicas.")


def clientes_del_catalogo(catalogo: CatalogoClientes) -> dict[str, tuple[FilaCliente, ...]]:
    """Reconstruye las celdas originales, deduplicando filas de cotitulares."""
    celdas: dict[str, dict[int, str]] = {hoja.nombre: {} for hoja in catalogo.hojas}
    for persona in catalogo.personas:
        for aparicion in persona.apariciones:
            ubicacion = aparicion.ubicacion
            anteriores = celdas[ubicacion.hoja]
            if (
                ubicacion.fila in anteriores
                and anteriores[ubicacion.fila] != aparicion.nombre_crudo
            ):
                raise ValidacionError("El catálogo atribuye textos distintos a una misma celda.")
            anteriores[ubicacion.fila] = aparicion.nombre_crudo
    return {
        nombre: tuple(
            FilaCliente(fila=fila, nombre_crudo=texto) for fila, texto in sorted(filas.items())
        )
        for nombre, filas in celdas.items()
    }


def construir_operaciones(
    *,
    catalogo: CatalogoClientes,
    semana: SemanaContactos,
    resolucion: ResolucionSemanal,
    encabezado: str,
    encabezados: Mapping[str, Encabezados],
    primeras_columnas: Mapping[str, int],
    permitir_pendientes: bool = False,
) -> tuple[MarcasHoja, ...]:
    """Agrupa destinos completos y selecciona una semana existente o la siguiente.

    El encabezado es un identificador explícito del llamador, no una fecha que se
    pueda interpretar. Los pendientes bloquean por defecto un reemplazo semanal
    incompleto; su inclusión parcial requiere ``permitir_pendientes=True``.
    """
    if not encabezado.strip() or encabezado != encabezado.strip():
        raise ValidacionError("Use un encabezado semanal canónico, sin espacios exteriores.")
    if resolucion.pendientes and not permitir_pendientes:
        raise ValidacionError("Revise los pendientes antes de planificar el reemplazo semanal.")
    resultados: tuple[ResultadoContacto, ...] = (
        *resolucion.resueltos,
        *resolucion.pendientes,
        *resolucion.ignorados,
    )
    contactos = Counter(semana.contactos)
    presentes = Counter(resultado.contacto for resultado in resultados)
    if set(contactos) != set(presentes) or any(
        presentes[contacto] < cantidad for contacto, cantidad in contactos.items()
    ):
        raise ValidacionError("La resolución debe cubrir los contactos de la semana recibida.")
    if not catalogo.hojas or set(encabezados) != {hoja.nombre for hoja in catalogo.hojas}:
        raise ValidacionError("Se requieren encabezados de todas las hojas del catálogo.")
    personas = {persona.persona_id: persona for persona in catalogo.personas}
    filas: dict[str, set[int]] = {hoja.nombre: set() for hoja in catalogo.hojas}
    for resuelto in resolucion.resueltos:
        persona = personas.get(resuelto.persona_id)
        if persona is None or resuelto.contacto.es_grupo:
            raise ValidacionError("El resultado resuelto no identifica una persona registrable.")
        hojas_elegidas = {ubicacion.hoja for ubicacion in resuelto.ubicaciones}
        esperadas = {
            ubicacion for ubicacion in persona.ubicaciones if ubicacion.hoja in hojas_elegidas
        }
        if set(resuelto.ubicaciones) != esperadas:
            raise ValidacionError(
                "El resultado debe conservar todas las filas de las hojas elegidas."
            )
        for ubicacion in resuelto.ubicaciones:
            filas[ubicacion.hoja].add(ubicacion.fila)
    operaciones: list[MarcasHoja] = []
    for hoja in catalogo.hojas:
        primera = primeras_columnas.get(hoja.nombre)
        if primera is None or type(primera) is not int or primera <= hoja.col_nombre:
            raise ValidacionError("Configure la primera columna semanal de cada hoja.")
        existentes = encabezados[hoja.nombre]
        columnas = tuple(columna for columna, _ in existentes)
        if (
            columnas != tuple(sorted(set(columnas)))
            or any(
                type(columna) is not int or columna < primera or not texto.strip()
                for columna, texto in existentes
            )
            or (columnas[0] if columnas else None) != hoja.primera_col_semana
            or (columnas[-1] if columnas else None) != hoja.ultima_col_semana
        ):
            raise ValidacionError("Los encabezados no corresponden a la estructura del catálogo.")
        coincidencias = tuple(
            columna for columna, texto in existentes if texto.strip() == encabezado
        )
        if len(coincidencias) > 1:
            raise ValidacionError("El encabezado solicitado aparece en más de una columna.")
        columna = coincidencias[0] if coincidencias else (columnas[-1] + 1 if columnas else primera)
        operaciones.append(
            MarcasHoja(
                hoja=hoja.nombre,
                columna=columna,
                encabezado=encabezado,
                filas=frozenset(filas[hoja.nombre]),
                nueva_semana=not coincidencias,
            )
        )
    return tuple(operaciones)
