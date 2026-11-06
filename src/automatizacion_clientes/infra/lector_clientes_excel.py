"""Lectura tipada de una instantánea XLSX sin modificar el archivo original."""

import logging
from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from types import TracebackType
from typing import Final, Self
from zipfile import BadZipFile

from openpyxl import load_workbook
from openpyxl.cell.read_only import EmptyCell, ReadOnlyCell
from openpyxl.styles import PatternFill
from openpyxl.utils.exceptions import InvalidFileException
from openpyxl.worksheet._read_only import ReadOnlyWorksheet

from automatizacion_clientes.core.errores import (
    ConfiguracionError,
    EstructuraError,
    PersistenciaError,
)
from automatizacion_clientes.core.modelos import FilaCliente, HojaInfo

LOGGER = logging.getLogger(__name__)
HOJAS_ESPERADAS: Final[tuple[str, ...]] = ("Inviu", "IOL", "Bull", "Balanz", "PPI")
FILA_ENCABEZADO_SEMANA: Final[int] = 2
FILA_INICIO_CLIENTES: Final[int] = 3
COLOR_VERDE_RGB: Final[str] = "76923C"
MAXIMA_COLUMNA_EXCEL: Final[int] = 16384


@dataclass(frozen=True, slots=True)
class _HojaLeida:
    info: HojaInfo
    clientes: tuple[FilaCliente, ...]
    ultimo_encabezado: str
    encabezados: tuple[tuple[int, str], ...]
    marcas: dict[int, frozenset[int]]
    columnas_color_no_admitido: frozenset[int]


class LectorClientesExcel:
    """Implementa ``LectorClientes`` sobre las cinco hojas del proyecto.

    Lee bytes, calcula su revisión y analiza el libro en memoria en modo
    read_only, sin guardar. Reutiliza el análisis solo si el contenido no cambió.
    Las marcas usan relleno sólido RGB verde o la letra histórica ``x``. Los
    colores de tema/indexados requieren conversión a RGB antes de leer marcas;
    no impiden construir el catálogo de clientes.
    """

    def __init__(self, ruta: Path) -> None:
        """Configura la ruta sin abrir el archivo ni crear directorios.

        Args:
            ruta: Archivo XLSX, normalmente ``Config.excel_path``.

        Raises:
            ConfiguracionError: La extensión no es XLSX.
        """
        if ruta.suffix.lower() != ".xlsx":
            raise ConfiguracionError("El lector de Excel requiere un archivo XLSX.")
        self._ruta = ruta
        self._cerrado = False
        self._revision_cache: str | None = None
        self._hojas_cache: dict[str, _HojaLeida] = {}

    def listar_hojas(self) -> tuple[HojaInfo, ...]:
        """Devuelve las hojas requeridas, con su índice real en el libro."""
        return tuple(hoja.info for hoja in self._obtener_hojas().values())

    def leer_clientes(self, hoja: str) -> tuple[FilaCliente, ...]:
        """Devuelve todos los nombres originales, conservando filas tras huecos."""
        return self._obtener_hoja(hoja).clientes

    def leer_marcas(self, hoja: str, columna: int) -> frozenset[int]:
        """Devuelve marcas solo de clientes y dentro de las columnas semanales.

        Raises:
            EstructuraError: Columna inválida o algún relleno sólido de esa
                columna usa un color de tema/indexado que no puede clasificarse.
        """
        self._asegurar_abierto()
        if type(columna) is not int or not 1 <= columna <= MAXIMA_COLUMNA_EXCEL:
            raise EstructuraError("La columna de marcas debe ser un índice válido de Excel.")
        leida = self._obtener_hoja(hoja)
        primera = leida.info.primera_col_semana
        ultima = leida.info.ultima_col_semana
        if primera is None or ultima is None or not primera <= columna <= ultima:
            raise EstructuraError("La columna solicitada no pertenece a las semanas existentes.")
        if columna in leida.columnas_color_no_admitido:
            raise EstructuraError(
                "Convierta los rellenos semanales de tema/indexados a colores RGB."
            )
        return leida.marcas.get(columna, frozenset())

    def ultima_semana(self, hoja: str) -> tuple[int, str]:
        """Devuelve el encabezado sin interpretar fechas; sin semanas devuelve A."""
        leida = self._obtener_hoja(hoja)
        columna = leida.info.ultima_col_semana
        return (1, "") if columna is None else (columna, leida.ultimo_encabezado)

    def revision(self) -> str:
        """Calcula SHA-256 sobre el archivo actual, incluyendo estilos y fórmulas."""
        return sha256(self._leer_contenido()).hexdigest()

    def leer_encabezados(self, hoja: str) -> tuple[tuple[int, str], ...]:
        """Devuelve todos los encabezados semanales sin perder columnas o espacios."""
        return self._obtener_hoja(hoja).encabezados

    def cerrar(self) -> None:
        """Libera la instantánea y bloquea nuevas lecturas; admite repetición."""
        self._hojas_cache.clear()
        self._revision_cache = None
        self._cerrado = True

    def __enter__(self) -> Self:
        """Permite gestionar la vida de la instantánea con ``with``."""
        self._asegurar_abierto()
        return self

    def __exit__(
        self,
        tipo_error: type[BaseException] | None,
        error: BaseException | None,
        traza: TracebackType | None,
    ) -> None:
        """Cierra la instantánea sin ocultar la excepción del bloque."""
        self.cerrar()

    def _asegurar_abierto(self) -> None:
        if self._cerrado:
            raise ConfiguracionError("El lector de Excel está cerrado. Cree otra instancia.")

    def _leer_contenido(self) -> bytes:
        self._asegurar_abierto()
        try:
            return self._ruta.read_bytes()
        except OSError as causa:
            raise PersistenciaError("No se pudo leer el archivo de clientes Excel.") from causa

    def _obtener_hoja(self, nombre: str) -> _HojaLeida:
        hojas = self._obtener_hojas()
        if nombre not in hojas:
            raise EstructuraError("La hoja solicitada no es una hoja de clientes configurada.")
        return hojas[nombre]

    def _obtener_hojas(self) -> dict[str, _HojaLeida]:
        contenido = self._leer_contenido()
        revision = sha256(contenido).hexdigest()
        if revision != self._revision_cache:
            try:
                hojas = _analizar_libro(contenido)
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
                    "El archivo no contiene un libro XLSX válido y compatible."
                ) from causa
            self._hojas_cache = hojas
            self._revision_cache = revision
            LOGGER.debug("Instantánea Excel validada: %s hojas.", len(hojas))
        return self._hojas_cache


def _analizar_libro(contenido: bytes) -> dict[str, _HojaLeida]:
    with BytesIO(contenido) as flujo:
        libro = load_workbook(flujo, read_only=True, data_only=False, keep_links=False)
        try:
            if not set(HOJAS_ESPERADAS) <= set(libro.sheetnames):
                raise EstructuraError(
                    "El libro no contiene todas las hojas de clientes requeridas."
                )
            resultado: dict[str, _HojaLeida] = {}
            for nombre in HOJAS_ESPERADAS:
                hoja = libro[nombre]
                if not isinstance(hoja, ReadOnlyWorksheet):
                    raise EstructuraError("Una hoja de clientes no es una hoja tabular.")
                hoja.reset_dimensions()
                resultado[nombre] = _analizar_hoja(hoja, libro.sheetnames.index(nombre))
            return resultado
        finally:
            libro.close()


def _analizar_hoja(hoja: ReadOnlyWorksheet, sheet_id: int) -> _HojaLeida:
    clientes: list[FilaCliente] = []
    encabezados: dict[int, str] = {}
    marcas: dict[int, set[int]] = {}
    colores_no_admitidos: set[int] = set()
    encabezado_nombre_valido = False
    primera_columna = 4 if hoja.title == "Balanz" else 3
    with closing(hoja.rows) as filas:
        for numero_fila, fila in enumerate(filas, start=1):
            celda_nombre = _celda(fila, 1)
            if numero_fila == 1:
                valor = None if celda_nombre is None else celda_nombre.value
                encabezado_nombre_valido = (
                    isinstance(valor, str)
                    and valor.strip().casefold() in {"cliente", "clientes", "nombre"}
                    and celda_nombre is not None
                    and celda_nombre.data_type not in {"f", "e"}
                )
            elif numero_fila == FILA_ENCABEZADO_SEMANA:
                for columna in range(primera_columna, len(fila) + 1):
                    celda = _celda(fila, columna)
                    if celda is not None and celda.value is not None and str(celda.value).strip():
                        if celda.data_type in {"f", "e"}:
                            raise EstructuraError(
                                "Los encabezados semanales no pueden ser fórmulas o errores."
                            )
                        encabezados[columna] = str(celda.value)
            elif numero_fila >= FILA_INICIO_CLIENTES:
                valor = None if celda_nombre is None else celda_nombre.value
                if valor is None or (isinstance(valor, str) and not valor.strip()):
                    continue
                if (
                    not isinstance(valor, str)
                    or celda_nombre is None
                    or celda_nombre.data_type in {"f", "e"}
                ):
                    raise EstructuraError(
                        "Los nombres de clientes deben ser texto literal, sin fórmulas o errores."
                    )
                clientes.append(FilaCliente(fila=numero_fila, nombre_crudo=valor))
                if encabezados:
                    for columna in range(min(encabezados), max(encabezados) + 1):
                        marca = _clasificar_marca(_celda(fila, columna))
                        if marca is None:
                            colores_no_admitidos.add(columna)
                        elif marca:
                            marcas.setdefault(columna, set()).add(numero_fila)
    if not encabezado_nombre_valido:
        raise EstructuraError("La columna A debe tener un encabezado Cliente, Clientes o Nombre.")
    primera = min(encabezados) if encabezados else None
    ultima = max(encabezados) if encabezados else None
    return _HojaLeida(
        info=HojaInfo(
            nombre=hoja.title,
            sheet_id=sheet_id,
            col_nombre=1,
            primera_col_semana=primera,
            ultima_col_semana=ultima,
            ultima_fila_con_cliente=clientes[-1].fila if clientes else FILA_INICIO_CLIENTES - 1,
        ),
        clientes=tuple(clientes),
        ultimo_encabezado="" if ultima is None else encabezados[ultima],
        encabezados=tuple(encabezados.items()),
        marcas={columna: frozenset(filas_marcadas) for columna, filas_marcadas in marcas.items()},
        columnas_color_no_admitido=frozenset(colores_no_admitidos),
    )


def _celda(fila: tuple[object, ...], columna: int) -> ReadOnlyCell | None:
    if columna > len(fila):
        return None
    celda = fila[columna - 1]
    if isinstance(celda, EmptyCell):
        return None
    if not isinstance(celda, ReadOnlyCell):
        raise EstructuraError("El lector recibió un tipo de celda XLSX incompatible.")
    return celda


def _clasificar_marca(celda: ReadOnlyCell | None) -> bool | None:
    if celda is None:
        return False
    if (
        isinstance(celda.value, str)
        and celda.data_type != "f"
        and celda.value.strip().casefold() == "x"
    ):
        return True
    relleno = celda.fill
    if not isinstance(relleno, PatternFill) or relleno.patternType != "solid":
        return False
    if relleno.fgColor.type != "rgb":
        return None
    color = relleno.fgColor.rgb
    return isinstance(color, str) and color[-6:].upper() == COLOR_VERDE_RGB
