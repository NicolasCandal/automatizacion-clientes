"""Puertos tipados que conectan el dominio con los adaptadores externos.

Son contratos estructurales: un adaptador los satisface mediante sus firmas,
sin heredar de ellos. No incluyen clientes de Google, librerías de Excel ni
implementaciones de red o archivos. Mypy verifica la compatibilidad; estos
protocolos no hacen comprobaciones de tipos en tiempo de ejecución.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Protocol

from .importacion_whatsapp import ExtraccionContactos
from .modelos import (
    AliasContacto,
    CatalogoClientes,
    FilaCliente,
    HojaInfo,
    PeriodoSemanal,
    ResolucionSemanal,
    SemanaContactos,
)


class Reloj(Protocol):
    """Fuente de instantes para construir resultados reproducibles en pruebas."""

    def ahora(self) -> datetime:
        """Devuelve un instante con zona horaria; no una fecha de calendario."""
        ...


class LectorClientes(Protocol):
    """Lectura de una planilla sin permitir escrituras desde este contrato.

    Las implementaciones traducen errores de estructura a ``EstructuraError``
    y fallos técnicos a las categorías de ``InfraestructuraError``. Filas y
    columnas usan índices desde uno, también para Google Sheets.
    """

    def listar_hojas(self) -> tuple[HojaInfo, ...]:
        """Devuelve las hojas configuradas y su estructura, en orden estable."""
        ...

    def leer_clientes(self, hoja: str) -> tuple[FilaCliente, ...]:
        """Devuelve las filas con cliente en orden, conservando el nombre original.

        Raises:
            EstructuraError: Si la hoja no existe o no tiene la estructura requerida.
        """
        ...

    def leer_marcas(self, hoja: str, columna: int) -> frozenset[int]:
        """Devuelve filas marcadas por color o por la marca histórica de texto."""
        ...

    def ultima_semana(self, hoja: str) -> tuple[int, str]:
        """Devuelve la columna y el encabezado original, sin interpretar sus fechas.

        Si todavía no hay semanas, devuelve la columna de nombres y una cadena
        vacía. El escritor debe respetar además las columnas fijas del origen;
        este valor no basta para deducir la primera columna escribible.
        """
        ...

    def revision(self) -> str:
        """Devuelve una revisión opaca del origen, incluyendo las marcas y el formato.

        No se interpreta como fecha ni se compara por orden. Una revisión
        distinta invalida la instantánea anterior; los servicios deben volver
        a leer cuando detecten un cambio.
        """
        ...


class LectorRegistroClientes(LectorClientes, Protocol):
    """Lectura adicional necesaria para planificar semanas existentes o nuevas."""

    def leer_encabezados(self, hoja: str) -> tuple[tuple[int, str], ...]:
        """Devuelve columnas y textos originales de todas las semanas, en orden."""
        ...


class EscritorClientes(Protocol):
    """Operaciones de escritura; el servicio decide cuándo respaldar y aplicar.

    Tener acceso a este contrato no ejecuta acciones. La simulación se
    implementa en el servicio omitiendo tanto respaldos como escrituras.
    """

    def crear_respaldo(self) -> Path | str:
        """Copia el estado sin modificarlo y devuelve su ubicación.

        Returns:
            Una ruta ``Path`` para respaldos locales o una URL para respaldos
            remotos. Las rutas locales nunca se devuelven como cadenas.

        Raises:
            PersistenciaError: Si no se puede crear una copia local.
            InfraestructuraError: Si no se puede completar un respaldo remoto.
        """
        ...

    def aplicar_marcas(
        self,
        hoja: str,
        columna: int,
        encabezado: str,
        filas: frozenset[int],
        *,
        revision_esperada: str,
    ) -> None:
        """Actualiza una semana tras comprobar las precondiciones de escritura.

        El adaptador comprueba revisión y estructura inmediatamente antes de
        escribir. Repetir el mismo encabezado y conjunto de filas actualiza la
        misma semana. Las demás filas de esa columna quedan sin marca; no se
        modifican fórmulas, columnas fijas ni otras semanas.

        La comprobación de revisión no promete una transacción ni un bloqueo
        distribuido. El adaptador documenta las garantías reales de su origen.
        Tras cada escritura propia, el servicio debe actualizar la revisión y
        revalidar lo que falte aplicar. Un timeout no permite repetir a ciegas.

        Args:
            hoja: Hoja de destino.
            columna: Columna planificada, numerada desde uno.
            encabezado: Identificador canónico de la semana.
            filas: Todas las filas que deben quedar marcadas en esa semana.
            revision_esperada: Revisión sobre la que se validó la operación.

        Raises:
            ConcurrenciaError: Si el origen cambió respecto de lo esperado.
            EstructuraError: Si el destino afecta columnas o filas no permitidas.
            InfraestructuraError: Si la escritura no se puede completar.
        """
        ...


class RepositorioClientes(LectorClientes, EscritorClientes, Protocol):
    """Repositorio completo que se construye en el punto de entrada.

    Los servicios reciben ``LectorClientes`` o ``EscritorClientes`` cuando no
    necesitan todas las capacidades. Quien construye el repositorio se encarga
    de cerrarlo con ``finally`` o con el contexto que ofrezca el adaptador.
    """

    def cerrar(self) -> None:
        """Libera libros, archivos y conexiones; permite llamadas repetidas.

        No guarda cambios pendientes ni aplica marcas implícitamente. Después
        del cierre el servicio no debe reutilizar el repositorio.
        """
        ...


class LectorContactos(Protocol):
    """Fuente de contactos semanales, configurada al construir el adaptador."""

    def leer(self) -> SemanaContactos:
        """Devuelve contactos validados con su período inclusivo.

        El adaptador convierte fechas y tipos al modelo. No devuelve contenido
        de mensajes ni registra contactos en la planilla.

        Raises:
            ValidacionError: Si los valores de entrada son inconsistentes.
            EstructuraError: Si faltan campos o el documento tiene un formato inválido.
            InfraestructuraError: Si no puede accederse a la fuente.
        """
        ...


class AlmacenAliases(Protocol):
    """Identidades aprendidas que se pueden consultar sin ejecutar el comparador.

    Los aliases guardan personas y hojas, sin cachear números de fila. El
    servicio valida las referencias contra el catálogo actual antes de usarlo.
    """

    def buscar_por_nombre(self, nombre_crudo: str) -> AliasContacto | None:
        """Busca un nombre original exacto; devuelve ``None`` si no hay alias."""
        ...

    def buscar_por_telefono(self, telefono: str) -> tuple[AliasContacto, ...]:
        """Devuelve todos los aliases del teléfono, o una tupla vacía.

        Un número puede estar compartido. El servicio compara los destinos y
        resuelve las discrepancias; el almacén no elige el primer alias.
        """
        ...

    def registrar(self, alias: AliasContacto) -> str | None:
        """Incorpora una resolución a la sesión sin sobrescribir conflictos.

        Una entrada compatible puede completar un teléfono ausente. Si cambia
        el teléfono ya conocido o los destinos del alias, conserva la entrada
        anterior y devuelve un aviso apto para el informe, sin datos personales.
        En caso de éxito devuelve ``None``. Repetir una entrada es idempotente.
        La persistencia explícita se realiza con ``guardar``.
        """
        ...

    def guardar(self) -> None:
        """Persiste las entradas aceptadas o lanza ``PersistenciaError``.

        La escritura local debe ser atómica para evitar archivos parciales.
        Un fallo no se presenta como éxito ni se descartan cambios en silencio.
        """
        ...


class ListaExclusion(Protocol):
    """Contactos excluidos explícitamente del registro de clientes."""

    def contiene(self, nombre_crudo: str) -> bool:
        """Comprueba pertenencia usando la normalización de nombres del dominio."""
        ...

    def agregar(self, nombre_crudo: str) -> None:
        """Añade y persiste una exclusión; repetirla no crea duplicados.

        Raises:
            ValidacionError: Si el nombre no contiene texto significativo.
            PersistenciaError: Si la exclusión no se puede guardar.
        """
        ...


class ComparadorNombres(Protocol):
    """Comparación numérica sin decisiones de aceptación ni reglas de brokers."""

    def similitud(self, nombre_consulta: str, nombre_candidato: str) -> float:
        """Compara nombres previamente normalizados y devuelve un valor de 0 a 100.

        La puntuación debe ser finita. La implementación no consulta aliases,
        no filtra por broker ni decide si el resultado requiere revisión.
        """
        ...


class AlmacenCatalogo(Protocol):
    """Persistencia de la instantánea usada como caché del catálogo."""

    def leer(self) -> CatalogoClientes | None:
        """Devuelve el catálogo validado o ``None`` si no existe.

        Un archivo corrupto produce ``EstructuraError`` o ``ValidacionError``;
        el servicio decide si reconstruirlo. Otros fallos de acceso producen
        ``PersistenciaError``. El almacén no valida la revisión contra la red.
        """
        ...

    def guardar(self, catalogo: CatalogoClientes) -> None:
        """Guarda la instantánea de forma atómica o lanza ``PersistenciaError``."""
        ...


class AlmacenResoluciones(Protocol):
    """Persistencia del informe de coincidencias, independiente de la planilla."""

    def leer(self) -> ResolucionSemanal:
        """Carga un informe validado; la ausencia del archivo es ``PersistenciaError``.

        Un formato incorrecto produce ``EstructuraError`` y las relaciones
        inválidas producen ``ValidacionError``.
        """
        ...

    def guardar(self, resolucion: ResolucionSemanal) -> None:
        """Guarda el informe de forma atómica o lanza ``PersistenciaError``."""
        ...


class ExtractorContactos(Protocol):
    """Extracción completa de un lote de chats para un período explícito."""

    def extraer(self, periodo: PeriodoSemanal) -> ExtraccionContactos:
        """Valida todas las entradas antes de devolver los contactos semanales."""
        ...


class AlmacenSemanaContactos(Protocol):
    """Destino del documento semanal consumido por el flujo de coincidencias."""

    def guardar(self, extraccion: ExtraccionContactos, *, extraido_en: datetime) -> None:
        """Guarda una extracción completa con su instante consciente de zona."""
        ...
