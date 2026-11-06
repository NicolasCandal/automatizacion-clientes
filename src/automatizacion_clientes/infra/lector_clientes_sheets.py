"""Instantáneas de lectura de Google Sheets con reintentos acotados y errores seguros."""

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from math import isfinite
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
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from automatizacion_clientes.config import Config
from automatizacion_clientes.core.errores import (
    AutenticacionError,
    AutorizacionError,
    ConfiguracionError,
    EstructuraError,
    ServicioExternoError,
    TiempoEsperaError,
    TransitorioError,
)
from automatizacion_clientes.core.modelos import FilaCliente, HojaInfo

LOGGER = logging.getLogger(__name__)
HOJAS_ESPERADAS: Final[tuple[str, ...]] = ("Inviu", "IOL", "Bull", "Balanz", "PPI")
CAMPOS_LECTURA: Final[str] = (
    "spreadsheetId,properties(spreadsheetTheme,defaultFormat),"
    "sheets(properties,data,merges,conditionalFormats,protectedRanges,bandedRanges,tables)"
)
type Objeto = dict[str, object]


class SesionSheets(Protocol):
    """Transporte autorizado inyectable, sin acceso a archivos de credenciales."""

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str | list[str]],
        timeout: float,
        allow_redirects: bool,
    ) -> Response:
        """Realiza una única consulta con límite de tiempo explícito."""
        ...

    def close(self) -> None:
        """Libera las conexiones de la sesión cuando su propietario lo solicita."""
        ...


class _FabricaSesion(Protocol):
    def __call__(
        self,
        credenciales: Credentials,
        *,
        max_refresh_attempts: int,
        refresh_timeout: float,
        auth_request: SolicitudCredenciales,
    ) -> SesionSheets: ...


@dataclass(slots=True)
class _SesionPropia:
    autorizada: SesionSheets
    renovacion: Session

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str | list[str]],
        timeout: float,
        allow_redirects: bool,
    ) -> Response:
        return self.autorizada.get(
            url,
            params=params,
            timeout=timeout,
            allow_redirects=allow_redirects,
        )

    def close(self) -> None:
        try:
            self.autorizada.close()
        finally:
            self.renovacion.close()


@dataclass(frozen=True, slots=True)
class _HojaLeida:
    info: HojaInfo
    clientes: tuple[FilaCliente, ...]
    encabezados: tuple[tuple[int, str], ...]
    marcas: dict[int, frozenset[int]]
    colores_no_resueltos: frozenset[int]


class LectorClientesSheets:
    """Implementa ``LectorRegistroClientes`` con una instantánea de cinco hojas.

    Las consultas de datos reutilizan la instantánea. ``revision`` siempre hace
    una consulta nueva y reemplaza la instantánea solo tras validarla; un fallo
    la invalida. La revisión es un hash del contenido y formato solicitado, no
    una fecha de Drive ni una garantía de bloqueo o de transacción remota.
    """

    def __init__(
        self,
        spreadsheet_id: str,
        *,
        sesion: SesionSheets,
        timeout_segundos: float = 30.0,
        intentos: int = 4,
        espera_minima_segundos: float = 1.0,
        espera_maxima_segundos: float = 30.0,
        cerrar_sesion: bool = False,
    ) -> None:
        """Configura la lectura sin red, archivos, descubrimiento ni autenticación.

        Args:
            spreadsheet_id: Identificador del documento; se codifica como segmento URL.
            sesion: Transporte ya configurado; no se cierra por defecto.
            timeout_segundos: Límite de conexión/lectura de cada intento HTTP.
            intentos: Total de intentos, incluido el primero, entre uno y diez.
            espera_minima_segundos: Piso de las esperas exponenciales con aleatoriedad.
            espera_maxima_segundos: Tope de esas esperas.
            cerrar_sesion: Hace que el lector sea propietario de la sesión recibida.

        Raises:
            ConfiguracionError: Identificador, esperas, intentos o timeout inválidos.
        """
        if not spreadsheet_id.strip():
            raise ConfiguracionError("Se necesita un identificador de Google Sheets.")
        if type(intentos) is not int or not 1 <= intentos <= 10:
            raise ConfiguracionError("Configure entre uno y diez intentos de lectura.")
        for valor in (timeout_segundos, espera_minima_segundos, espera_maxima_segundos):
            if isinstance(valor, bool) or not isfinite(valor) or valor <= 0:
                raise ConfiguracionError("Los tiempos de lectura deben ser positivos y finitos.")
        if espera_minima_segundos > espera_maxima_segundos:
            raise ConfiguracionError("La espera mínima no puede superar la máxima.")
        self._identificador = spreadsheet_id.strip()
        self._url = "https://sheets.googleapis.com/v4/spreadsheets/" + quote(
            self._identificador, safe=""
        )
        self._sesion = sesion
        self._timeout = timeout_segundos
        self._intentos = intentos
        self._espera_minima = espera_minima_segundos
        self._espera_maxima = espera_maxima_segundos
        self._cerrar_sesion = cerrar_sesion
        self._cerrado = False
        self._hojas: dict[str, _HojaLeida] | None = None

    def listar_hojas(self) -> tuple[HojaInfo, ...]:
        """Entrega las cinco hojas en orden estable y con sus IDs reales."""
        return tuple(hoja.info for hoja in self._obtener_hojas().values())

    def leer_clientes(self, hoja: str) -> tuple[FilaCliente, ...]:
        """Conserva nombres originales y todas las filas, incluidos los huecos."""
        return self._obtener_hoja(hoja).clientes

    def leer_encabezados(self, hoja: str) -> tuple[tuple[int, str], ...]:
        """Entrega columnas y textos de todas las semanas sin recortar espacios."""
        return self._obtener_hoja(hoja).encabezados

    def ultima_semana(self, hoja: str) -> tuple[int, str]:
        """Devuelve la última semana o ``(1, '')`` cuando no hay encabezados."""
        encabezados = self._obtener_hoja(hoja).encabezados
        return encabezados[-1] if encabezados else (1, "")

    def leer_marcas(self, hoja: str, columna: int) -> frozenset[int]:
        """Reconoce verde efectivo o ``x`` literal, solo en filas de clientes.

        Un color de tema que no se pueda resolver invalida la consulta de esa
        columna, sin impedir leer los clientes o construir el catálogo.
        """
        if type(columna) is not int or columna < 1:
            raise EstructuraError("La columna de marcas debe ser un entero positivo.")
        leida = self._obtener_hoja(hoja)
        primera, ultima = leida.info.primera_col_semana, leida.info.ultima_col_semana
        if primera is None or ultima is None or not primera <= columna <= ultima:
            raise EstructuraError("La columna no pertenece a las semanas existentes.")
        if columna in leida.colores_no_resueltos:
            raise EstructuraError("No se pudo resolver el color de tema de una marca semanal.")
        return leida.marcas.get(columna, frozenset())

    def revision(self) -> str:
        """Actualiza desde la API y calcula SHA-256 incluyendo celdas y formato."""
        return self.leer_instantanea()[0]

    def leer_instantanea(self) -> tuple[str, dict[str, object]]:
        """Descarga y valida una instantánea nueva para respaldos y precondiciones.

        Returns:
            Revisión y documento JSON de las cinco hojas solicitadas. No es una
            exportación completa de Drive. El documento no comparte objetos
            mutables con las estructuras internas del lector.
        """
        self._asegurar_abierto()
        self._hojas = None
        documento = self._consultar()
        try:
            contenido = json.dumps(
                documento,
                sort_keys=True,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (ValueError, TypeError, RecursionError) as causa:
            raise EstructuraError("La respuesta de Sheets no contiene JSON compatible.") from causa
        hojas = _analizar_documento(documento, self._identificador)
        revision = sha256(contenido).hexdigest()
        self._hojas = hojas
        LOGGER.debug("Instantánea Sheets validada: %s hojas.", len(hojas))
        return revision, cast(dict[str, object], documento)

    def cerrar(self) -> None:
        """Invalida el lector y libera únicamente una sesión de su propiedad."""
        if self._cerrado:
            return
        self._cerrado = True
        self._hojas = None
        if self._cerrar_sesion:
            try:
                self._sesion.close()
            except OSError as causa:
                raise ServicioExternoError("No se pudo cerrar la sesión de Sheets.") from causa

    def __enter__(self) -> Self:
        """Permite administrar la instantánea y, si corresponde, la sesión."""
        self._asegurar_abierto()
        return self

    def __exit__(
        self,
        tipo_error: type[BaseException] | None,
        error: BaseException | None,
        traza: TracebackType | None,
    ) -> None:
        """Cierra sin ocultar las excepciones del bloque."""
        try:
            self.cerrar()
        except ServicioExternoError:
            if error is None:
                raise
            LOGGER.warning("No se pudo cerrar la sesión de Sheets tras un fallo previo.")

    def _asegurar_abierto(self) -> None:
        if self._cerrado:
            raise ConfiguracionError("El lector de Sheets está cerrado. Cree otra instancia.")

    def _obtener_hojas(self) -> dict[str, _HojaLeida]:
        self._asegurar_abierto()
        if self._hojas is None:
            self.revision()
        if self._hojas is None:
            raise EstructuraError("No se pudo construir la instantánea de Sheets.")
        return self._hojas

    def _obtener_hoja(self, nombre: str) -> _HojaLeida:
        if nombre not in HOJAS_ESPERADAS:
            raise EstructuraError("La hoja solicitada no es una hoja de clientes configurada.")
        return self._obtener_hojas()[nombre]

    def _consultar(self) -> object:
        for intento in Retrying(
            retry=retry_if_exception_type(TransitorioError),
            stop=stop_after_attempt(self._intentos),
            wait=wait_random_exponential(
                multiplier=self._espera_minima, min=self._espera_minima, max=self._espera_maxima
            ),
            before_sleep=_avisar_reintento,
            reraise=True,
        ):
            with intento:
                return self._descargar()
        raise ServicioExternoError("No se pudo completar la lectura de Sheets.")

    def _descargar(self) -> object:
        try:
            respuesta = self._sesion.get(
                self._url,
                params={
                    "ranges": [f"'{nombre}'" for nombre in HOJAS_ESPERADAS],
                    "fields": CAMPOS_LECTURA,
                },
                timeout=self._timeout,
                allow_redirects=False,
            )
            try:
                validar_estado_http_sheets(respuesta)
                try:
                    documento: object = respuesta.json()
                except ValueError as causa:
                    raise EstructuraError(
                        "La respuesta de Sheets no contiene JSON válido."
                    ) from causa
                return documento
            finally:
                respuesta.close()
        except SSLError as causa:
            raise ServicioExternoError(
                "No se pudo verificar la conexión TLS con Sheets."
            ) from causa
        except Timeout as causa:
            raise TiempoEsperaError("La lectura de Sheets excedió el tiempo permitido.") from causa
        except (ConnectionError, ChunkedEncodingError, TransportError) as causa:
            raise TransitorioError("La conexión con Sheets falló temporalmente.") from causa
        except RefreshError as causa:
            if getattr(causa, "retryable", False) is True:
                raise TransitorioError(
                    "La renovación de credenciales falló temporalmente."
                ) from causa
            raise AutenticacionError("Las credenciales de Google requieren renovación.") from causa
        except GoogleAuthError as causa:
            raise AutenticacionError("No se pudo autorizar la lectura de Google Sheets.") from causa
        except RequestException as causa:
            raise ServicioExternoError("No se pudo completar la consulta de Sheets.") from causa


def crear_lector_sheets(configuracion: Config, credenciales: Credentials) -> LectorClientesSheets:
    """Construye una sesión propia con credenciales ya cargadas, sin abrir archivos.

    Los flujos OAuth y la carga de credenciales quedan fuera de este módulo.
    Configure el alcance ``spreadsheets.readonly`` en quien autoriza. Se desactiva
    la repetición automática tras HTTP 401; Tenacity controla los reintentos de
    lectura. El timeout de renovación también se configura explícitamente.
    """
    if configuracion.spreadsheet_id is None:
        raise ConfiguracionError("Configure el identificador de Google Sheets.")
    # Los stubs de google-auth no tipan su constructor; se acota solo esta frontera.
    fabricar_sesion = cast(_FabricaSesion, AuthorizedSession)
    # Session usa adaptadores sin reintentos: tampoco se multiplican al renovar.
    renovacion = Session()
    try:
        sesion = fabricar_sesion(
            credenciales,
            max_refresh_attempts=0,
            refresh_timeout=configuracion.timeout_red_segundos,
            auth_request=SolicitudCredenciales(session=renovacion),
        )
    except Exception:
        renovacion.close()
        raise
    return LectorClientesSheets(
        configuracion.spreadsheet_id,
        sesion=_SesionPropia(autorizada=sesion, renovacion=renovacion),
        cerrar_sesion=True,
        timeout_segundos=configuracion.timeout_red_segundos,
        intentos=configuracion.reintentos_maximos,
        espera_minima_segundos=configuracion.reintento_espera_minima_segundos,
        espera_maxima_segundos=configuracion.reintento_espera_maxima_segundos,
    )


def validar_estado_http_sheets(respuesta: Response) -> None:
    """Traduce estados HTTP sin exponer el cuerpo, identificadores o secretos."""
    estado = respuesta.status_code
    if 200 <= estado < 300:
        return
    if estado == 408:
        raise TiempoEsperaError("La API de Sheets agotó el tiempo de la consulta.")
    if estado == 429 or (500 <= estado < 600 and estado not in {501, 505}):
        raise TransitorioError("La API de Sheets está temporalmente indisponible.")
    if estado == 401:
        raise AutenticacionError("Google Sheets rechazó las credenciales.")
    if estado == 403:
        try:
            contenido: object = respuesta.json()
        except ValueError:
            contenido = None
        if _es_cuota_temporal(contenido):
            raise TransitorioError("La cuota de lecturas de Sheets está temporalmente agotada.")
        raise AutorizacionError("La identidad no tiene autorización para consultar Sheets.")
    if estado == 404:
        raise AutorizacionError("La planilla solicitada no existe o no es accesible.")
    if estado == 400:
        raise EstructuraError("La API de Sheets rechazó la estructura de la consulta.")
    raise ServicioExternoError("La API de Sheets rechazó la consulta.")


def _es_cuota_temporal(contenido: object) -> bool:
    if not isinstance(contenido, dict):
        return False
    error = cast(dict[str, object], contenido).get("error")
    if not isinstance(error, dict):
        return False
    motivos = cast(dict[str, object], error).get("errors")
    if not isinstance(motivos, list):
        return False
    razones = [
        cast(dict[str, object], motivo).get("reason")
        for motivo in motivos
        if isinstance(motivo, dict)
    ]
    return bool(razones) and all(
        isinstance(razon, str) and razon in {"rateLimitExceeded", "userRateLimitExceeded"}
        for razon in razones
    )


def _avisar_reintento(estado: RetryCallState) -> None:
    LOGGER.warning(
        "Lectura Sheets transitoria; se reintentará tras el intento %s.", estado.attempt_number
    )


def _objeto(valor: object) -> Objeto:
    if not isinstance(valor, dict) or any(not isinstance(clave, str) for clave in valor):
        raise EstructuraError("Se esperaba un objeto en la respuesta de Sheets.")
    return cast(Objeto, valor)


def _lista(valor: object) -> list[object]:
    if not isinstance(valor, list):
        raise EstructuraError("Se esperaba una lista en la respuesta de Sheets.")
    return cast(list[object], valor)


def _entero(valor: object, *, minimo: int) -> int:
    if type(valor) is not int or valor < minimo:
        raise EstructuraError("Un índice o dimensión de Sheets no es válido.")
    return valor


def _analizar_documento(documento: object, identificador: str) -> dict[str, _HojaLeida]:
    raiz = _objeto(documento)
    if raiz.get("spreadsheetId") != identificador:
        raise EstructuraError("La respuesta no corresponde a la planilla solicitada.")
    hojas: dict[str, Objeto] = {}
    identificadores: set[int] = set()
    for elemento in _lista(raiz.get("sheets")):
        hoja = _objeto(elemento)
        propiedades = _objeto(hoja.get("properties"))
        nombre = propiedades.get("title")
        if not isinstance(nombre, str):
            raise EstructuraError("La respuesta de Sheets contiene una hoja sin nombre válido.")
        if nombre in hojas:
            raise EstructuraError("La respuesta de Sheets contiene nombres de hoja duplicados.")
        identificador_hoja = _entero(propiedades.get("sheetId"), minimo=0)
        if identificador_hoja in identificadores:
            raise EstructuraError("La respuesta de Sheets contiene identificadores duplicados.")
        identificadores.add(identificador_hoja)
        hojas[nombre] = hoja
    if not set(HOJAS_ESPERADAS) <= hojas.keys():
        raise EstructuraError("La planilla no contiene todas las hojas de clientes requeridas.")
    temas = _obtener_temas(raiz)
    formato_default = _objeto(_objeto(raiz.get("properties", {})).get("defaultFormat", {}))
    return {
        nombre: _analizar_hoja(hojas[nombre], temas, formato_default) for nombre in HOJAS_ESPERADAS
    }


def _analizar_hoja(
    hoja: Objeto,
    temas: Mapping[str, tuple[float, float, float]],
    formato_default: Objeto,
) -> _HojaLeida:
    propiedades = _objeto(hoja.get("properties"))
    nombre = propiedades["title"]
    if not isinstance(nombre, str) or propiedades.get("sheetType", "GRID") != "GRID":
        raise EstructuraError("Las hojas de clientes deben ser cuadrículas tabulares.")
    dimensiones = _objeto(propiedades.get("gridProperties"))
    total_filas = _entero(dimensiones.get("rowCount"), minimo=2)
    total_columnas = _entero(dimensiones.get("columnCount"), minimo=1)
    celdas = _obtener_celdas(hoja, total_filas, total_columnas)
    titulo = _texto_literal(celdas.get((1, 1), {}), nombre=True)
    if titulo is None or titulo.strip().casefold() not in {"cliente", "clientes", "nombre"}:
        raise EstructuraError("La columna A debe tener un encabezado de clientes válido.")
    clientes: list[FilaCliente] = []
    encabezados: list[tuple[int, str]] = []
    primera_columna = 4 if nombre == "Balanz" else 3
    for (fila, columna), celda in sorted(celdas.items()):
        if fila == 2 and columna >= primera_columna:
            texto = _texto_literal(celda, nombre=False)
            if texto is not None and texto.strip():
                encabezados.append((columna, texto))
        elif fila >= 3 and columna == 1:
            texto = _texto_literal(celda, nombre=True)
            if texto is not None and texto.strip():
                clientes.append(FilaCliente(fila=fila, nombre_crudo=texto))
    primera = encabezados[0][0] if encabezados else None
    ultima = encabezados[-1][0] if encabezados else None
    filas_clientes = {cliente.fila for cliente in clientes}
    marcas: dict[int, set[int]] = {}
    no_resueltos: set[int] = set()
    for (fila, columna), celda in celdas.items():
        if (
            fila not in filas_clientes
            or primera is None
            or ultima is None
            or not primera <= columna <= ultima
        ):
            continue
        marca = _clasificar_marca(celda, temas, formato_default)
        if marca is None:
            no_resueltos.add(columna)
        elif marca:
            marcas.setdefault(columna, set()).add(fila)
    return _HojaLeida(
        info=HojaInfo(
            nombre=nombre,
            sheet_id=_entero(propiedades["sheetId"], minimo=0),
            col_nombre=1,
            primera_col_semana=primera,
            ultima_col_semana=ultima,
            ultima_fila_con_cliente=clientes[-1].fila if clientes else 2,
        ),
        clientes=tuple(clientes),
        encabezados=tuple(encabezados),
        marcas={columna: frozenset(filas) for columna, filas in marcas.items()},
        colores_no_resueltos=frozenset(no_resueltos),
    )


def _obtener_celdas(
    hoja: Objeto, total_filas: int, total_columnas: int
) -> dict[tuple[int, int], Objeto]:
    celdas: dict[tuple[int, int], Objeto] = {}
    for elemento in _lista(hoja.get("data", [])):
        bloque = _objeto(elemento)
        inicio_fila = _entero(bloque.get("startRow", 0), minimo=0)
        inicio_columna = _entero(bloque.get("startColumn", 0), minimo=0)
        if inicio_fila >= total_filas or inicio_columna >= total_columnas:
            raise EstructuraError("Un bloque de Sheets comienza fuera de la cuadrícula.")
        for desplazamiento_fila, elemento_fila in enumerate(_lista(bloque.get("rowData", []))):
            fila = inicio_fila + desplazamiento_fila + 1
            fila_cruda = _objeto(elemento_fila)
            if fila > total_filas:
                raise EstructuraError("Una fila de Sheets supera sus dimensiones declaradas.")
            for desplazamiento_columna, elemento_celda in enumerate(
                _lista(fila_cruda.get("values", []))
            ):
                columna = inicio_columna + desplazamiento_columna + 1
                if columna > total_columnas or (fila, columna) in celdas:
                    raise EstructuraError(
                        "Una celda de Sheets está duplicada o fuera de la cuadrícula."
                    )
                celdas[(fila, columna)] = _objeto(elemento_celda)
    return celdas


def _texto_literal(celda: Objeto, *, nombre: bool) -> str | None:
    valor = _objeto(celda.get("userEnteredValue", {}))
    efectivo = _objeto(celda.get("effectiveValue", {}))
    if "formulaValue" in valor or "errorValue" in valor or "errorValue" in efectivo:
        raise EstructuraError("Los nombres y encabezados de clientes deben ser valores literales.")
    if not valor:
        if efectivo:
            raise EstructuraError("Una celda de clientes o encabezados carece de valor literal.")
        return None
    if len(valor) != 1 or not set(valor) <= {"stringValue", "numberValue", "boolValue"}:
        raise EstructuraError("Un valor literal de Sheets no tiene el tipo esperado.")
    if "stringValue" in valor:
        texto = valor["stringValue"]
        if not isinstance(texto, str):
            raise EstructuraError("El valor de texto de una celda de Sheets es inválido.")
        return texto
    if nombre:
        raise EstructuraError("Los nombres de clientes deben ser texto literal.")
    if "boolValue" in valor and type(valor["boolValue"]) is not bool:
        raise EstructuraError("Un encabezado booleano de Sheets tiene un valor inválido.")
    if "numberValue" in valor:
        numero = valor["numberValue"]
        if isinstance(numero, bool) or not isinstance(numero, (int, float)):
            raise EstructuraError("Un encabezado numérico de Sheets tiene un valor inválido.")
        try:
            finito = isfinite(numero)
        except OverflowError as causa:
            raise EstructuraError(
                "Un encabezado numérico de Sheets excede el rango admitido."
            ) from causa
        if not finito:
            raise EstructuraError("Un encabezado numérico de Sheets debe ser finito.")
    formateado = celda.get("formattedValue")
    if formateado is not None:
        if not isinstance(formateado, str):
            raise EstructuraError("El encabezado formateado de Sheets debe ser texto.")
        return formateado
    return str(next(iter(valor.values())))


def _color_rgb(valor: object) -> tuple[float, float, float]:
    color = _objeto(valor)
    canales: list[float] = []
    for canal in ("red", "green", "blue"):
        componente = color.get(canal, 0.0)
        if (
            isinstance(componente, bool)
            or not isinstance(componente, (int, float))
            or not 0 <= componente <= 1
            or not isfinite(componente)
        ):
            raise EstructuraError("Un componente de color de Sheets es inválido.")
        canales.append(float(componente))
    return canales[0], canales[1], canales[2]


def _obtener_temas(raiz: Objeto) -> dict[str, tuple[float, float, float]]:
    propiedades = _objeto(raiz.get("properties", {}))
    tema = _objeto(propiedades.get("spreadsheetTheme", {}))
    temas: dict[str, tuple[float, float, float]] = {}
    for elemento in _lista(tema.get("themeColors", [])):
        pareja = _objeto(elemento)
        tipo = pareja.get("colorType")
        color = _objeto(pareja.get("color"))
        if (
            not isinstance(tipo, str)
            or not tipo.strip()
            or tipo in temas
            or "rgbColor" not in color
        ):
            raise EstructuraError("La tabla de colores del tema de Sheets es inválida.")
        temas[tipo] = _color_rgb(color["rgbColor"])
    return temas


def _clasificar_marca(
    celda: Objeto,
    temas: Mapping[str, tuple[float, float, float]],
    formato_default: Objeto,
) -> bool | None:
    literal = _objeto(celda.get("userEnteredValue", {})).get("stringValue")
    if isinstance(literal, str) and literal.strip().casefold() == "x":
        return True
    formato = _objeto(celda.get("effectiveFormat", {}))
    if not {"backgroundColorStyle", "backgroundColor"} & formato.keys():
        formato = formato_default
    if "backgroundColorStyle" in formato:
        estilo = _objeto(formato["backgroundColorStyle"])
        if "rgbColor" in estilo and "themeColor" in estilo:
            raise EstructuraError("Un color de Sheets no puede tener dos representaciones.")
        if "rgbColor" in estilo:
            color = _color_rgb(estilo["rgbColor"])
        elif "themeColor" in estilo:
            tipo = estilo["themeColor"]
            if not isinstance(tipo, str):
                raise EstructuraError("El nombre de color de tema de Sheets es inválido.")
            if tipo not in temas:
                return None
            color = temas[tipo]
        else:
            return False
    elif "backgroundColor" in formato:
        color = _color_rgb(formato["backgroundColor"])
    else:
        return False
    verde = (118 / 255, 146 / 255, 60 / 255)
    return all(abs(canal - esperado) <= 0.01 for canal, esperado in zip(color, verde, strict=True))
