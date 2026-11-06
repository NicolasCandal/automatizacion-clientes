"""Respaldos y escritura de semanas XLSX con revisión optimista del origen."""

import logging
from copy import copy
from datetime import UTC
from hashlib import sha256
from io import BytesIO
from os import fsync
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import cast
from zipfile import BadZipFile, ZipFile

from openpyxl import Workbook, load_workbook
from openpyxl.cell.cell import Cell
from openpyxl.cell.rich_text import CellRichText
from openpyxl.descriptors.serialisable import Serialisable
from openpyxl.styles import PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.utils.exceptions import InvalidFileException
from openpyxl.worksheet.worksheet import Worksheet
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from automatizacion_clientes.core.contratos import Reloj
from automatizacion_clientes.core.errores import (
    ConcurrenciaError,
    ConfiguracionError,
    EstructuraError,
    PersistenciaError,
)
from automatizacion_clientes.infra.lector_clientes_excel import (
    COLOR_VERDE_RGB,
    FILA_ENCABEZADO_SEMANA,
    FILA_INICIO_CLIENTES,
    HOJAS_ESPERADAS,
    MAXIMA_COLUMNA_EXCEL,
)

LOGGER = logging.getLogger(__name__)


class EscritorClientesExcel:
    """Implementa ``EscritorClientes`` sin mantener libros o archivos abiertos.

    El servicio debe crear el respaldo antes de aplicar y refrescar la revisión
    tras cada escritura. La comprobación SHA-256 es optimista: requiere un único
    escritor y no constituye un bloqueo ni una transacción entre varias hojas.
    """

    def __init__(self, ruta: Path, *, directorio_respaldos: Path, reloj: Reloj) -> None:
        """Recibe rutas y reloj sin acceder al disco.

        Args:
            ruta: Libro XLSX, normalmente ``Config.excel_path``.
            directorio_respaldos: Carpeta de copias independientes del original.
            reloj: Fuente de instantes conscientes para nombrar las copias.

        Raises:
            ConfiguracionError: La extensión no es XLSX.
        """
        if ruta.suffix.lower() != ".xlsx":
            raise ConfiguracionError("El escritor de Excel requiere un archivo XLSX.")
        self._ruta = ruta
        self._directorio_respaldos = directorio_respaldos
        self._reloj = reloj

    def crear_respaldo(self) -> Path:
        """Copia exactamente los bytes actuales a un nombre exclusivo.

        No abre ni vuelve a serializar el libro. Dos copias con el mismo instante
        reciben nombres distintos; un fallo elimina solo la copia incompleta.
        Un cambio del origen durante la copia produce ``ConcurrenciaError``.

        Returns:
            Ruta del respaldo cerrado y sincronizado.

        Raises:
            ConfiguracionError: El reloj devuelve un instante sin zona horaria.
            ConcurrenciaError: El archivo cambió mientras se respaldaba.
            PersistenciaError: Falló la lectura o la escritura de la copia.
        """
        instante = self._reloj.ahora()
        if instante.tzinfo is None or instante.utcoffset() is None:
            raise ConfiguracionError("El reloj del respaldo debe incluir zona horaria.")
        prefijo = instante.astimezone(UTC).strftime("clientes_%Y%m%dT%H%M%S_%fZ_")
        contenido = self._leer_contenido()
        revision = sha256(contenido).hexdigest()
        respaldo: Path | None = None
        completado = False
        try:
            self._directorio_respaldos.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(
                mode="wb",
                dir=self._directorio_respaldos,
                prefix=prefijo,
                suffix=".xlsx",
                delete=False,
            ) as archivo:
                respaldo = Path(archivo.name)
                archivo.write(contenido)
                archivo.flush()
                fsync(archivo.fileno())
            self._comprobar_revision(revision)
            completado = True
        except OSError as causa:
            raise PersistenciaError("No se pudo crear el respaldo Excel.") from causa
        finally:
            if respaldo is not None and not completado:
                _eliminar_temporal(respaldo)
        LOGGER.info("Respaldo Excel creado y sincronizado.")
        return respaldo

    def aplicar_marcas(
        self,
        hoja: str,
        columna: int,
        encabezado: str,
        filas: frozenset[int],
        *,
        revision_esperada: str,
    ) -> None:
        """Actualiza una semana existente o añade la siguiente, sin insertar columnas.

        La columna debe coincidir con el encabezado existente (comparado sin
        espacios exteriores) o ser la inmediatamente posterior a la última
        semana. Sin semanas, empieza en C, o D para Balanz. Nunca redirige una
        petición a otra columna. Solo admite celdas semanales vacías o ``x``;
        fórmulas, datos ajenos, celdas combinadas y hojas protegidas se rechazan.

        Los clientes seleccionados quedan verdes y los demás blancos, sin ``x``.
        Conserva huecos, fórmulas, columnas fijas, otras semanas y estilos salvo
        el relleno de marcas. Las filas sin cliente no se modifican.

        Args:
            hoja: Una de las cinco hojas de clientes.
            columna: Índice desde uno de la semana a actualizar o añadir.
            encabezado: Identificador literal, no una fórmula ni un error Excel.
            filas: Todas las filas de clientes que deben quedar verdes.
            revision_esperada: SHA-256 obtenido con ``LectorClientesExcel``.

        Raises:
            ConcurrenciaError: Cambió la revisión inicial o anterior al reemplazo.
            EstructuraError: Petición o libro incompatibles con la escritura.
            PersistenciaError: Falló la lectura, serialización o sustitución.
        """
        _validar_peticion(hoja, columna, encabezado, filas, revision_esperada)
        contenido = self._leer_contenido()
        if sha256(contenido).hexdigest() != revision_esperada:
            raise ConcurrenciaError("El libro Excel cambió. Vuelva a leer y planificar.")
        try:
            _validar_componentes(contenido)
            with BytesIO(contenido) as flujo:
                libro = load_workbook(flujo, data_only=False, keep_links=True, rich_text=True)
                try:
                    destino, filas_clientes, nueva = _validar_destino(
                        libro,
                        hoja,
                        columna,
                        encabezado,
                        filas,
                    )
                    if nueva:
                        _copiar_formato_encabezado(destino, columna)
                        destino.cell(FILA_ENCABEZADO_SEMANA, columna).value = encabezado.strip()
                    for fila in filas_clientes:
                        celda = destino.cell(fila, columna)
                        celda.value = None
                        celda.fill = PatternFill(
                            patternType="solid",
                            fgColor="FF" + COLOR_VERDE_RGB if fila in filas else "FFFFFFFF",
                        )
                    self._guardar(libro, revision_esperada)
                finally:
                    libro.close()
        except OSError as causa:
            raise PersistenciaError("No se pudo guardar el libro Excel.") from causa
        except (
            BadZipFile,
            InvalidFileException,
            SyntaxError,
            ValueError,
            KeyError,
            IndexError,
            TypeError,
        ) as causa:
            raise EstructuraError(
                "El libro XLSX no es válido o no admite esta operación."
            ) from causa
        LOGGER.info("Semana Excel actualizada: %s clientes marcados.", len(filas))

    def _leer_contenido(self) -> bytes:
        try:
            return self._ruta.read_bytes()
        except OSError as causa:
            raise PersistenciaError("No se pudo leer el archivo de clientes Excel.") from causa

    def _comprobar_revision(self, revision_esperada: str) -> None:
        if sha256(self._leer_contenido()).hexdigest() != revision_esperada:
            raise ConcurrenciaError("El libro Excel cambió. Vuelva a leer y planificar.")

    def _guardar(self, libro: Workbook, revision_esperada: str) -> None:
        temporal: Path | None = None
        try:
            with NamedTemporaryFile(
                mode="w+b",
                dir=self._ruta.parent,
                prefix=".excel-",
                suffix=".tmp",
                delete=False,
            ) as archivo:
                temporal = Path(archivo.name)
                archivo.write(_serializar(libro))
                archivo.flush()
                fsync(archivo.fileno())
            for intento in Retrying(
                retry=retry_if_exception(_es_bloqueo_temporal),
                stop=stop_after_attempt(4),
                wait=wait_exponential(multiplier=0.05, min=0.05, max=0.2),
                before_sleep=_avisar_reintento,
                reraise=True,
            ):
                with intento:
                    # También se repite la precondición después de un bloqueo.
                    self._comprobar_revision(revision_esperada)
                    temporal.replace(self._ruta)
            temporal = None
        finally:
            if temporal is not None:
                _eliminar_temporal(temporal)


def _validar_peticion(
    hoja: str,
    columna: int,
    encabezado: str,
    filas: frozenset[int],
    revision: str,
) -> None:
    if hoja not in HOJAS_ESPERADAS:
        raise EstructuraError("La hoja solicitada no es una hoja de clientes configurada.")
    if type(columna) is not int or not 1 <= columna <= MAXIMA_COLUMNA_EXCEL:
        raise EstructuraError("La columna semanal debe ser un índice válido de Excel.")
    if (
        not encabezado.strip()
        or len(encabezado.strip()) > 32767
        or encabezado.strip().startswith("=")
        or encabezado.strip()
        in {
            "#NULL!",
            "#DIV/0!",
            "#VALUE!",
            "#REF!",
            "#NAME?",
            "#NUM!",
            "#N/A",
        }
        or any(ord(caracter) < 32 and caracter not in "\t\n\r" for caracter in encabezado)
    ):
        raise EstructuraError("El encabezado semanal debe ser texto literal válido.")
    if any(type(fila) is not int or not 3 <= fila <= 1048576 for fila in filas):
        raise EstructuraError("Las filas de marcas deben ser índices válidos de clientes.")
    if not revision.strip():
        raise EstructuraError("La escritura requiere una revisión del origen.")


def _validar_componentes(contenido: bytes) -> None:
    # Estas partes no tienen conservación completa al reserializar con openpyxl.
    prefijos = (
        "xl/drawings/",
        "xl/vbaProject",
        "xl/activeX/",
        "xl/ctrlProps/",
        "xl/slicers/",
        "xl/slicerCaches/",
        "_xmlsignatures/",
    )
    with ZipFile(BytesIO(contenido)) as paquete:
        if any(nombre.startswith(prefijos) for nombre in paquete.namelist()):
            raise EstructuraError(
                "El escritor admite libros tabulares sin dibujos, macros, controles ni firmas."
            )


def _validar_destino(
    libro: Workbook,
    nombre: str,
    columna: int,
    encabezado: str,
    filas: frozenset[int],
) -> tuple[Worksheet, frozenset[int], bool]:
    if not set(HOJAS_ESPERADAS) <= set(libro.sheetnames):
        raise EstructuraError("El libro no contiene todas las hojas de clientes requeridas.")
    hoja = libro[nombre]
    if not isinstance(hoja, Worksheet) or hoja.protection.sheet:
        raise EstructuraError("La hoja de destino debe ser tabular y no estar protegida.")
    titulo = hoja.cell(1, 1)
    if (
        not isinstance(titulo.value, str)
        or titulo.data_type in {"f", "e"}
        or titulo.value.strip().casefold() not in {"cliente", "clientes", "nombre"}
    ):
        raise EstructuraError("La columna A debe tener un encabezado de clientes válido.")
    primera = 4 if nombre == "Balanz" else 3
    semanas: dict[int, str] = {}
    for indice in range(primera, hoja.max_column + 1):
        celda = hoja.cell(FILA_ENCABEZADO_SEMANA, indice)
        if celda.value is not None and str(celda.value).strip():
            if celda.data_type in {"f", "e"}:
                raise EstructuraError("Los encabezados semanales deben ser literales.")
            semanas[indice] = str(celda.value).strip()
    coincidencias = [indice for indice, texto in semanas.items() if texto == encabezado.strip()]
    nueva = not coincidencias
    siguiente = max(semanas) + 1 if semanas else primera
    if (not nueva and coincidencias != [columna]) or (nueva and columna != siguiente):
        raise EstructuraError("La columna no corresponde a la semana existente o siguiente.")
    if any(rango.min_col <= columna <= rango.max_col for rango in hoja.merged_cells.ranges):
        raise EstructuraError("La columna semanal no puede contener celdas combinadas.")
    clientes: set[int] = set()
    for indice in range(FILA_INICIO_CLIENTES, hoja.max_row + 1):
        celda_nombre = hoja.cell(indice, 1)
        valor = celda_nombre.value
        if valor is None or (isinstance(valor, (str, CellRichText)) and not str(valor).strip()):
            continue
        if not isinstance(valor, (str, CellRichText)) or celda_nombre.data_type in {"f", "e"}:
            raise EstructuraError("Los nombres de clientes deben ser texto literal.")
        celda = hoja.cell(indice, columna)
        if celda.data_type in {"f", "e"} or (
            celda.value is not None
            and not (isinstance(celda.value, str) and celda.value.strip().casefold() in {"", "x"})
        ):
            raise EstructuraError("La columna semanal contiene fórmulas o datos ajenos a marcas.")
        clientes.add(indice)
    if not filas <= clientes:
        raise EstructuraError("La selección incluye filas sin cliente en el origen actual.")
    return hoja, frozenset(clientes), nueva


def _copiar_formato_encabezado(hoja: Worksheet, columna: int) -> None:
    primera = 4 if hoja.title == "Balanz" else 3
    if columna <= primera:
        return
    origen = hoja.cell(FILA_ENCABEZADO_SEMANA, columna - 1)
    destino = hoja.cell(FILA_ENCABEZADO_SEMANA, columna)
    if not isinstance(origen, Cell) or not isinstance(destino, Cell):
        raise EstructuraError("El encabezado semanal no puede estar combinado.")
    # StyleProxy.__copy__ devuelve el estilo real, no otro proxy (stubs imprecisos).
    destino.font = cast(Serialisable, copy(origen.font))
    destino.fill = cast(Serialisable, copy(origen.fill))
    destino.border = cast(Serialisable, copy(origen.border))
    destino.alignment = cast(Serialisable, copy(origen.alignment))
    destino.protection = cast(Serialisable, copy(origen.protection))
    destino.number_format = origen.number_format
    letra_anterior = get_column_letter(columna - 1)
    if letra_anterior in hoja.column_dimensions:
        hoja.column_dimensions[get_column_letter(columna)].width = hoja.column_dimensions[
            letra_anterior
        ].width


def _serializar(libro: Workbook) -> bytes:
    with BytesIO() as flujo:
        libro.save(flujo)
        return flujo.getvalue()


def _es_bloqueo_temporal(causa: BaseException) -> bool:
    return isinstance(causa, OSError) and getattr(causa, "winerror", None) in {32, 33}


def _eliminar_temporal(ruta: Path) -> None:
    try:
        ruta.unlink(missing_ok=True)
    except OSError:
        LOGGER.warning("No se pudo eliminar un archivo temporal Excel tras un fallo.")


def _avisar_reintento(estado: RetryCallState) -> None:
    LOGGER.warning(
        "Reemplazo Excel bloqueado temporalmente; se reintentará tras el intento %s.",
        estado.attempt_number,
    )
