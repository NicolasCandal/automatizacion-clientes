"""Respaldos locales y marcas Sheets con lotes idempotentes y revisión optimista."""

import json
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC
from os import fsync
from pathlib import Path
from tempfile import NamedTemporaryFile
from types import TracebackType
from typing import Final, Protocol, Self, cast
from urllib.parse import quote

from google.auth.credentials import Credentials
from google.auth.exceptions import GoogleAuthError, RefreshError, TransportError
from google.auth.transport.requests import AuthorizedSession
from google.auth.transport.requests import Request as SolicitudCredenciales
from requests import Response, Session
from requests.exceptions import (
    ChunkedEncodingError,
    ConnectionError,
    RequestException,
    SSLError,
    Timeout,
)
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_random_exponential

from automatizacion_clientes.config import Config
from automatizacion_clientes.core.contratos import Reloj
from automatizacion_clientes.core.errores import (
    AutenticacionError,
    AutorizacionError,
    ConcurrenciaError,
    ConfiguracionError,
    EstructuraError,
    PersistenciaError,
    ServicioExternoError,
    TiempoEsperaError,
    TransitorioError,
)
from automatizacion_clientes.infra.lector_clientes_sheets import (
    HOJAS_ESPERADAS,
    LectorClientesSheets,
    SesionSheets,
    validar_estado_http_sheets,
)

LOGGER = logging.getLogger(__name__)
MAXIMA_COLUMNA: Final[int] = 18278
ALCANCES_ESCRITURA: Final[frozenset[str]] = frozenset(
    {
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.file",
        "https://www.googleapis.com/auth/drive",
    }
)
type Objeto = dict[str, object]


class SesionEscrituraSheets(SesionSheets, Protocol):
    """Transporte autorizado sin reintentos propios ni redirecciones implícitas."""

    def post(
        self,
        url: str,
        *,
        json: Mapping[str, object],
        timeout: float,
        allow_redirects: bool,
    ) -> Response:
        """Envía un único lote a la API de Sheets."""
        ...


class _FabricaSesion(Protocol):
    def __call__(
        self,
        credenciales: Credentials,
        *,
        max_refresh_attempts: int,
        refresh_timeout: float,
        auth_request: SolicitudCredenciales,
    ) -> Session: ...


class _CredencialesConAlcances(Protocol):
    def has_scopes(self, scopes: Sequence[str]) -> bool: ...


class EscritorClientesSheets:
    """Implementa ``EscritorClientes`` con un respaldo JSON y un lote por hoja.

    El servicio decide cuándo respaldar. Requiere un único escritor: la revisión
    no es una precondición atómica del servidor y puede cambiar entre GET y POST.
    No agrega, copia o inserta recursos en los reintentos; solo asigna valores y
    colores y, cuando falta espacio, un número absoluto de columnas.
    """

    def __init__(
        self,
        spreadsheet_id: str,
        *,
        sesion: SesionEscrituraSheets,
        directorio_respaldos: Path,
        reloj: Reloj,
        timeout_segundos: float = 30.0,
        intentos: int = 4,
        espera_minima_segundos: float = 1.0,
        espera_maxima_segundos: float = 30.0,
        cerrar_sesion: bool = False,
        sesion_renovacion: Session | None = None,
    ) -> None:
        """Recibe dependencias sin consultar red, credenciales ni archivos.

        Args:
            spreadsheet_id: Documento que debe coincidir con el lector del servicio.
            sesion: Sesión autorizada; el llamador administra permisos y transporte.
            directorio_respaldos: Destino local para instantáneas JSON exclusivas.
            reloj: Fuente de instantes con zona horaria para los respaldos.
            timeout_segundos: Timeout de conexión y lectura por petición.
            intentos: Intentos máximos, incluido el primero, entre uno y diez.
            espera_minima_segundos: Espera exponencial aleatoria mínima.
            espera_maxima_segundos: Espera exponencial aleatoria máxima.
            cerrar_sesion: Transfiere la propiedad de la sesión al escritor.
            sesion_renovacion: Sesión auxiliar propia de la fábrica, si existe.
        """
        self._lector = LectorClientesSheets(
            spreadsheet_id,
            sesion=sesion,
            timeout_segundos=timeout_segundos,
            intentos=intentos,
            espera_minima_segundos=espera_minima_segundos,
            espera_maxima_segundos=espera_maxima_segundos,
        )
        self._sesion = sesion
        self._renovacion = sesion_renovacion
        self._cerrar_sesion = cerrar_sesion
        self._cerrado = False
        self._identificador = spreadsheet_id.strip()
        self._url = (
            "https://sheets.googleapis.com/v4/spreadsheets/"
            + quote(self._identificador, safe="")
            + ":batchUpdate"
        )
        self._directorio_respaldos = directorio_respaldos
        self._reloj = reloj
        self._timeout = timeout_segundos
        self._intentos = intentos
        self._espera_minima = espera_minima_segundos
        self._espera_maxima = espera_maxima_segundos

    def crear_respaldo(self) -> Path:
        """Guarda y sincroniza las cinco hojas consultadas en un JSON exclusivo.

        Incluye revisión, instante UTC y respuesta de Sheets. No es una copia
        integral de Drive ni contiene un restaurador automático. Si cambia el
        origen durante el respaldo, elimina la copia y falla sin escribir Sheets.
        """
        self._asegurar_abierto()
        instante = self._reloj.ahora()
        if instante.tzinfo is None or instante.utcoffset() is None:
            raise ConfiguracionError("El reloj del respaldo debe incluir zona horaria.")
        instante = instante.astimezone(UTC)
        revision, documento = self._lector.leer_instantanea()
        contenido = json.dumps(
            {
                "version": 1,
                "creado_en": instante.isoformat(),
                "revision": revision,
                "documento": documento,
            },
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
        ).encode("utf-8")
        respaldo: Path | None = None
        completado = False
        try:
            self._directorio_respaldos.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(
                mode="wb",
                dir=self._directorio_respaldos,
                prefix=instante.strftime("sheets_%Y%m%dT%H%M%S_%fZ_"),
                suffix=".json",
                delete=False,
            ) as archivo:
                respaldo = Path(archivo.name)
                archivo.write(contenido)
                archivo.flush()
                fsync(archivo.fileno())
            if self._lector.revision() != revision:
                raise ConcurrenciaError("Sheets cambió durante el respaldo.")
            completado = True
        except OSError as causa:
            raise PersistenciaError("No se pudo guardar el respaldo local de Sheets.") from causa
        finally:
            if respaldo is not None and not completado:
                try:
                    respaldo.unlink(missing_ok=True)
                except OSError:
                    LOGGER.warning("No se pudo eliminar un respaldo incompleto de Sheets.")
        LOGGER.info("Respaldo local de Sheets creado y sincronizado.")
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
        """Asigna una semana existente o la siguiente con un lote atómico por hoja.

        Solo escribe celdas de clientes: elimina ``x`` y asigna verde o blanco.
        No toca huecos, fórmulas ajenas, estilos restantes ni otras semanas.
        Rechaza destinos con datos ajenos, fórmulas, combinaciones, protecciones,
        reglas condicionales o bandas superpuestas y hojas con tablas o pivotes.

        Antes de cada POST, incluido cada reintento, descarga la revisión. Un
        cambio detiene el envío. Un timeout puede haber aplicado el lote: el
        servicio debe informar estado incierto y replanificar, nunca restaurar
        o repetir todo el flujo automáticamente.
        """
        self._asegurar_abierto()
        _validar_peticion(hoja, columna, encabezado, filas, revision_esperada)
        envio_iniciado = False
        reintentar = retry_if_exception(
            lambda causa: envio_iniciado and isinstance(causa, TransitorioError)
        )
        for intento in Retrying(
            retry=reintentar,
            stop=stop_after_attempt(self._intentos),
            wait=wait_random_exponential(
                multiplier=self._espera_minima, min=self._espera_minima, max=self._espera_maxima
            ),
            reraise=True,
        ):
            with intento:
                envio_iniciado = False
                revision, documento = self._lector.leer_instantanea()
                if revision != revision_esperada:
                    raise ConcurrenciaError("Sheets cambió. Vuelva a leer y planificar.")
                peticiones = _preparar(self._lector, documento, hoja, columna, encabezado, filas)
                if peticiones:
                    if self._lector.revision() != revision_esperada:
                        raise ConcurrenciaError("Sheets cambió antes del envío del lote.")
                    envio_iniciado = True
                    self._enviar({"requests": peticiones, "includeSpreadsheetInResponse": False})
        LOGGER.info("Semana Sheets actualizada: %s clientes marcados.", len(filas))

    def _enviar(self, cuerpo: Mapping[str, object]) -> None:
        try:
            respuesta = self._sesion.post(
                self._url, json=cuerpo, timeout=self._timeout, allow_redirects=False
            )
            try:
                validar_estado_http_sheets(respuesta)
                try:
                    resultado = _objeto(respuesta.json())
                except ValueError as causa:
                    raise EstructuraError("Sheets no confirmó el lote con JSON válido.") from causa
                if resultado.get("spreadsheetId") != self._identificador:
                    raise EstructuraError("Sheets no confirmó el documento del lote aplicado.")
            finally:
                respuesta.close()
        except SSLError as causa:
            raise ServicioExternoError("No se pudo verificar TLS al escribir Sheets.") from causa
        except Timeout as causa:
            LOGGER.warning("Escritura Sheets sin confirmar; se comprobará la revisión.")
            raise TiempoEsperaError("La escritura Sheets excedió el tiempo permitido.") from causa
        except (ConnectionError, ChunkedEncodingError, TransportError) as causa:
            LOGGER.warning("Escritura Sheets sin confirmar; se comprobará la revisión.")
            raise TransitorioError(
                "La conexión de escritura Sheets falló temporalmente."
            ) from causa
        except RefreshError as causa:
            if getattr(causa, "retryable", False) is True:
                raise TransitorioError(
                    "La renovación de credenciales falló temporalmente."
                ) from causa
            raise AutenticacionError("Las credenciales de Google requieren renovación.") from causa
        except GoogleAuthError as causa:
            raise AutenticacionError("No se pudo autorizar la escritura Sheets.") from causa
        except RequestException as causa:
            raise ServicioExternoError("No se pudo completar el lote de Sheets.") from causa

    def cerrar(self) -> None:
        """Cierra recursos propios una sola vez e invalida el escritor."""
        if self._cerrado:
            return
        self._cerrado = True
        self._lector.cerrar()
        try:
            try:
                if self._cerrar_sesion:
                    self._sesion.close()
            finally:
                if self._renovacion is not None:
                    self._renovacion.close()
        except OSError as causa:
            raise ServicioExternoError(
                "No se pudo cerrar la sesión de escritura Sheets."
            ) from causa

    def _asegurar_abierto(self) -> None:
        if self._cerrado:
            raise ConfiguracionError("El escritor de Sheets está cerrado. Cree otra instancia.")

    def __enter__(self) -> Self:
        """Administra el ciclo de vida de las sesiones propias."""
        self._asegurar_abierto()
        return self

    def __exit__(
        self,
        tipo_error: type[BaseException] | None,
        error: BaseException | None,
        traza: TracebackType | None,
    ) -> None:
        """Libera recursos sin ocultar una excepción previa del bloque."""
        try:
            self.cerrar()
        except ServicioExternoError:
            if error is None:
                raise
            LOGGER.warning("No se pudo cerrar Sheets tras un fallo previo.")


def crear_escritor_sheets(
    configuracion: Config, credenciales: Credentials, *, reloj: Reloj
) -> EscritorClientesSheets:
    """Crea sesiones propias con permisos de escritura explícitos y sin login.

    No amplía alcances, persiste tokens ni abre navegador. El alcance inicial
    de solo lectura de Config se rechaza antes de crear las sesiones.
    """
    if configuracion.spreadsheet_id is None:
        raise ConfiguracionError("Configure el identificador de Google Sheets.")
    if not ALCANCES_ESCRITURA.intersection(configuracion.google_alcances):
        raise ConfiguracionError("Configure explícitamente un alcance de escritura de Sheets.")
    alcances = cast(_CredencialesConAlcances, credenciales)
    if not hasattr(credenciales, "has_scopes") or not any(
        alcances.has_scopes([alcance]) for alcance in ALCANCES_ESCRITURA
    ):
        raise AutorizacionError("Las credenciales no declaran permisos de escritura de Sheets.")
    renovacion = Session()
    autorizada: Session | None = None
    try:
        autorizada = cast(_FabricaSesion, AuthorizedSession)(
            credenciales,
            max_refresh_attempts=0,
            refresh_timeout=configuracion.timeout_red_segundos,
            auth_request=SolicitudCredenciales(session=renovacion),
        )
        return EscritorClientesSheets(
            configuracion.spreadsheet_id,
            sesion=autorizada,
            directorio_respaldos=configuracion.directorio_respaldos,
            reloj=reloj,
            timeout_segundos=configuracion.timeout_red_segundos,
            intentos=configuracion.reintentos_maximos,
            espera_minima_segundos=configuracion.reintento_espera_minima_segundos,
            espera_maxima_segundos=configuracion.reintento_espera_maxima_segundos,
            cerrar_sesion=True,
            sesion_renovacion=renovacion,
        )
    except Exception:
        try:
            if autorizada is not None:
                autorizada.close()
        finally:
            renovacion.close()
        raise


def _validar_peticion(
    hoja: str, columna: int, encabezado: str, filas: frozenset[int], revision: str
) -> None:
    if hoja not in HOJAS_ESPERADAS:
        raise EstructuraError("La hoja solicitada no es una hoja de clientes.")
    if type(columna) is not int or not 1 <= columna <= MAXIMA_COLUMNA:
        raise EstructuraError("La columna semanal debe ser un índice válido de Sheets.")
    if (
        not encabezado.strip()
        or len(encabezado.strip()) > 50000
        or encabezado.strip().startswith("=")
        or any(ord(caracter) < 32 and caracter not in "\t\n\r" for caracter in encabezado)
    ):
        raise EstructuraError("El encabezado semanal debe ser texto literal válido.")
    if any(type(fila) is not int or fila < 3 for fila in filas):
        raise EstructuraError("Las filas deben ser índices válidos de clientes.")
    if not revision.strip():
        raise EstructuraError("La escritura requiere una revisión del origen.")


def _preparar(
    lector: LectorClientesSheets,
    documento: Objeto,
    nombre: str,
    columna: int,
    encabezado: str,
    seleccionadas: frozenset[int],
) -> list[Objeto]:
    destino = next(
        _objeto(elemento)
        for elemento in _lista(documento["sheets"])
        if _objeto(_objeto(elemento)["properties"])["title"] == nombre
    )
    propiedades = _objeto(destino["properties"])
    identificador = cast(int, propiedades["sheetId"])
    dimensiones = _objeto(propiedades["gridProperties"])
    capacidad = cast(int, dimensiones["columnCount"])
    semanas = lector.leer_encabezados(nombre)
    coincidentes = [indice for indice, texto in semanas if texto.strip() == encabezado.strip()]
    nueva = not coincidentes
    siguiente = semanas[-1][0] + 1 if semanas else (4 if nombre == "Balanz" else 3)
    if (not nueva and coincidentes != [columna]) or (nueva and columna != siguiente):
        raise EstructuraError("La columna no corresponde a la semana existente o siguiente.")
    clientes = frozenset(cliente.fila for cliente in lector.leer_clientes(nombre))
    if not seleccionadas <= clientes:
        raise EstructuraError("La selección incluye filas sin cliente en el origen actual.")
    afectadas = clientes | ({2} if nueva else set())
    _validar_restricciones(destino, identificador, columna, afectadas)
    celdas = _celdas(destino)
    for fila in afectadas:
        actual = celdas.get((fila, columna), {})
        if actual.get("dataValidation"):
            raise EstructuraError("El destino contiene una validación de datos.")
        if fila != 2 or nueva:
            for campo in ("userEnteredValue", "effectiveValue"):
                valor = _objeto(actual.get(campo, {}))
                if valor and (
                    set(valor) != {"stringValue"}
                    or not isinstance(valor["stringValue"], str)
                    or valor["stringValue"].strip().casefold() not in {"", "x"}
                ):
                    raise EstructuraError("La semana contiene fórmulas o datos ajenos a marcas.")
    peticiones: list[Objeto] = []
    if columna > capacidad:
        peticiones.append(
            {
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": identificador,
                        "gridProperties": {"columnCount": columna},
                    },
                    "fields": "gridProperties.columnCount",
                }
            }
        )
    if nueva:
        peticiones.append(
            {
                "updateCells": {
                    "start": {"sheetId": identificador, "rowIndex": 1, "columnIndex": columna - 1},
                    "rows": [
                        {"values": [{"userEnteredValue": {"stringValue": encabezado.strip()}}]}
                    ],
                    "fields": "userEnteredValue",
                }
            }
        )
    for grupo in _agrupar(clientes):
        filas_json: list[Objeto] = []
        for fila in grupo:
            color = (
                {"red": 118 / 255, "green": 146 / 255, "blue": 60 / 255}
                if fila in seleccionadas
                else {"red": 1.0, "green": 1.0, "blue": 1.0}
            )
            filas_json.append(
                {"values": [{"userEnteredFormat": {"backgroundColorStyle": {"rgbColor": color}}}]}
            )
        peticiones.append(
            {
                "updateCells": {
                    "start": {
                        "sheetId": identificador,
                        "rowIndex": grupo[0] - 1,
                        "columnIndex": columna - 1,
                    },
                    "rows": filas_json,
                    "fields": (
                        "userEnteredValue,userEnteredFormat.backgroundColor,"
                        "userEnteredFormat.backgroundColorStyle"
                    ),
                }
            }
        )
    # Límite local conservador para conservar la atomicidad de un único lote.
    if len(json.dumps(peticiones, ensure_ascii=False).encode("utf-8")) > 1_900_000:
        raise EstructuraError("El lote excede el límite local; requiere otro modo de escritura.")
    return peticiones


def _validar_restricciones(
    hoja: Objeto, identificador: int, columna: int, filas: frozenset[int]
) -> None:
    if hoja.get("tables"):
        raise EstructuraError("El escritor requiere una hoja sin tablas de Sheets.")
    rangos = list(_lista(hoja.get("merges", [])))
    for campo in ("protectedRanges", "bandedRanges"):
        rangos.extend(
            _objeto(elemento).get("range", {}) for elemento in _lista(hoja.get(campo, []))
        )
    for regla in _lista(hoja.get("conditionalFormats", [])):
        rangos.extend(_lista(_objeto(regla).get("ranges", [{}])) or [{}])
    if any(_intersecta(_objeto(rango), identificador, columna, filas) for rango in rangos):
        raise EstructuraError("La semana intersecta formato automático, protección o combinación.")


def _intersecta(rango: Objeto, identificador: int, columna: int, filas: frozenset[int]) -> bool:
    if rango.get("sheetId", identificador) != identificador:
        raise EstructuraError("Un rango de la hoja tiene una referencia incompatible.")
    indices: dict[str, int] = {}
    for campo in ("startRowIndex", "endRowIndex", "startColumnIndex", "endColumnIndex"):
        valor = rango.get(campo)
        if valor is not None:
            if type(valor) is not int or valor < 0:
                raise EstructuraError("Un rango tiene índices incompatibles.")
            indices[campo] = valor
    for eje in ("Row", "Column"):
        if indices.get(f"end{eje}Index", 2**63) <= indices.get(f"start{eje}Index", 0):
            raise EstructuraError("Un rango tiene límites incompatibles.")
    return indices.get("startColumnIndex", 0) <= columna - 1 < indices.get(
        "endColumnIndex", 2**63
    ) and any(
        indices.get("startRowIndex", 0) <= fila - 1 < indices.get("endRowIndex", 2**63)
        for fila in filas
    )


def _celdas(hoja: Objeto) -> dict[tuple[int, int], Objeto]:
    resultado: dict[tuple[int, int], Objeto] = {}
    for bloque in _lista(hoja.get("data", [])):
        cuadricula = _objeto(bloque)
        inicio_fila = cast(int, cuadricula.get("startRow", 0))
        inicio_columna = cast(int, cuadricula.get("startColumn", 0))
        for desplazamiento_fila, fila in enumerate(_lista(cuadricula.get("rowData", [])), 1):
            for desplazamiento_columna, celda in enumerate(
                _lista(_objeto(fila).get("values", [])), 1
            ):
                contenido = _objeto(celda)
                if any(
                    campo in contenido
                    for campo in ("pivotTable", "dataSourceTable", "dataSourceFormula")
                ):
                    raise EstructuraError(
                        "El escritor requiere una hoja sin pivotes ni fuentes de datos."
                    )
                resultado[
                    (inicio_fila + desplazamiento_fila, inicio_columna + desplazamiento_columna)
                ] = contenido
    return resultado


def _agrupar(filas: frozenset[int]) -> list[list[int]]:
    grupos: list[list[int]] = []
    for fila in sorted(filas):
        if not grupos or fila != grupos[-1][-1] + 1:
            grupos.append([])
        grupos[-1].append(fila)
    return grupos


def _objeto(valor: object) -> Objeto:
    if not isinstance(valor, dict):
        raise EstructuraError("Sheets devolvió un objeto incompatible con la escritura.")
    return cast(Objeto, valor)


def _lista(valor: object) -> list[object]:
    if not isinstance(valor, list):
        raise EstructuraError("Sheets devolvió una colección incompatible con la escritura.")
    return cast(list[object], valor)
