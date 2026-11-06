"""Manifiestos explícitos y lectura acotada de exportaciones locales de WhatsApp."""

import json
import logging
from collections.abc import Generator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from stat import S_ISREG
from typing import cast

from automatizacion_clientes.core.errores import (
    ConcurrenciaError,
    ConfiguracionError,
    EstructuraError,
    PersistenciaError,
    ValidacionError,
)
from automatizacion_clientes.core.importacion_whatsapp import (
    ExtraccionContactos,
    OrdenFecha,
    analizar_actividad,
)
from automatizacion_clientes.core.modelos import ContactoSemanal, PeriodoSemanal, SemanaContactos

LOGGER = logging.getLogger(__name__)
type Objeto = dict[str, object]
type Huella = tuple[int, int, int, int, int]


@dataclass(frozen=True, slots=True, kw_only=True, repr=False)
class _Chat:
    ruta: Path
    nombre: str
    telefono: str | None
    es_grupo: bool
    remitentes: tuple[str, ...]


class ExtractorWhatsAppTxt:
    """Implementa ``ExtractorContactos`` sin acceder a WhatsApp Web ni a la red.

    Cada archivo corresponde a un chat cuya identidad, grupo y remitentes están
    declarados en el manifiesto. No deduce clientes a partir de nombres de archivo
    ni conserva cuerpos de mensajes. Un fallo de cualquier entrada invalida el
    lote completo; no se omiten archivos con errores.
    """

    def __init__(
        self,
        manifiesto: Path,
        *,
        limite_chats: int = 500,
        limite_archivo_bytes: int = 25 * 1024 * 1024,
        limite_linea_bytes: int = 256 * 1024,
        limite_total_bytes: int = 100 * 1024 * 1024,
    ) -> None:
        """Configura rutas y límites sin abrir archivos ni crear directorios.

        Args:
            manifiesto: JSON con orden de fecha y lista explícita de chats.
            limite_chats: Máximo de archivos por lote.
            limite_archivo_bytes: Máximo de bytes de cada TXT.
            limite_linea_bytes: Máximo de bytes de cada línea, incluido el salto.
            limite_total_bytes: Máximo de bytes de los TXT del lote completo.
        """
        if any(
            type(limite) is not int or limite <= 0
            for limite in (
                limite_chats,
                limite_archivo_bytes,
                limite_linea_bytes,
                limite_total_bytes,
            )
        ):
            raise ConfiguracionError("Los límites de importación deben ser enteros positivos.")
        self._manifiesto = manifiesto.expanduser().resolve()
        self._limite_chats = limite_chats
        self._limite_archivo = limite_archivo_bytes
        self._limite_linea = limite_linea_bytes
        self._limite_total = limite_total_bytes

    def extraer(self, periodo: PeriodoSemanal) -> ExtraccionContactos:
        """Lee y valida todos los TXT antes de entregar una semana completa.

        Las rutas deben ser relativas, únicas y resolver dentro de la carpeta
        del manifiesto. Los archivos deben ser regulares, TXT y UTF-8. Se
        comprueban metadatos antes y después de cada lectura y al terminar el
        lote. Esto detecta cambios habituales; no bloquea archivos ni ofrece una
        instantánea atómica. Los archivos deben permanecer sin modificaciones.

        Raises:
            EstructuraError: Esquema, formato, codificación o límites incompatibles.
            ConcurrenciaError: Cambió un archivo durante la extracción.
            PersistenciaError: No se pudo leer un recurso local.
        """
        inicial = _huella(self._manifiesto)
        documento = _leer_manifiesto(self._manifiesto)
        chats, orden, siglo = self._interpretar(documento)
        huellas = {chat.ruta: _huella(chat.ruta) for chat in chats}
        if (
            any(huella[2] > self._limite_archivo for huella in huellas.values())
            or sum(huella[2] for huella in huellas.values()) > self._limite_total
        ):
            raise EstructuraError("Las exportaciones exceden los límites de tamaño del lote.")
        contactos: list[ContactoSemanal] = []
        mensajes = 0
        for chat in chats:
            with closing(self._lineas(chat.ruta)) as lineas:
                actividad = analizar_actividad(
                    lineas,
                    periodo=periodo,
                    remitentes=chat.remitentes,
                    orden_fecha=orden,
                    siglo_anio_corto=siglo,
                )
            _verificar_huella(chat.ruta, huellas[chat.ruta])
            mensajes += actividad.mensajes_reconocidos
            if actividad.ultima_fecha is not None:
                contactos.append(
                    ContactoSemanal(
                        nombre_crudo=chat.nombre,
                        telefono=chat.telefono,
                        es_grupo=chat.es_grupo,
                        ultima_fecha=actividad.ultima_fecha,
                    )
                )
        _verificar_huella(self._manifiesto, inicial)
        for ruta, huella in huellas.items():
            _verificar_huella(ruta, huella)
        resultado = ExtraccionContactos(
            semana=SemanaContactos(periodo=periodo, contactos=tuple(contactos)),
            total_chats_escaneados=len(chats),
            mensajes_reconocidos=mensajes,
        )
        LOGGER.info(
            "Importación TXT validada: %s chats; %s contactos en el período.",
            len(chats),
            len(contactos),
        )
        return resultado

    def _interpretar(self, documento: Objeto) -> tuple[tuple[_Chat, ...], OrdenFecha, int | None]:
        if (
            not {"version_esquema", "orden_fecha", "chats"}
            <= documento.keys()
            <= {
                "version_esquema",
                "orden_fecha",
                "chats",
                "siglo_anio_corto",
            }
            or type(documento["version_esquema"]) is not int
            or documento["version_esquema"] != 1
        ):
            raise EstructuraError("El manifiesto tiene campos o una versión incompatibles.")
        orden = documento["orden_fecha"]
        siglo = documento.get("siglo_anio_corto")
        if orden not in ("dia_mes", "mes_dia") or (
            siglo is not None and (type(siglo) is not int or siglo not in (1900, 2000))
        ):
            raise EstructuraError("El manifiesto debe declarar un orden de fecha y siglo válidos.")
        entradas = documento["chats"]
        if not isinstance(entradas, list) or not 1 <= len(entradas) <= self._limite_chats:
            raise EstructuraError("El manifiesto debe contener un lote no vacío dentro del límite.")
        chats: list[_Chat] = []
        for entrada in cast(list[object], entradas):
            chat = _interpretar_chat(entrada, self._manifiesto.parent)
            if chat.ruta in {anterior.ruta for anterior in chats}:
                raise EstructuraError("El manifiesto repite un archivo exportado.")
            chats.append(chat)
        return tuple(chats), orden, siglo

    def _lineas(self, ruta: Path) -> Generator[str, None, None]:
        total = 0
        try:
            with ruta.open("rb") as archivo:
                while contenido := archivo.readline(
                    min(self._limite_linea, self._limite_archivo) + 1
                ):
                    total += len(contenido)
                    if len(contenido) > self._limite_linea or total > self._limite_archivo:
                        raise EstructuraError(
                            "Una exportación excede el límite de archivo o línea."
                        )
                    yield contenido.decode("utf-8-sig")
        except UnicodeError as causa:
            raise EstructuraError("Las exportaciones deben estar codificadas en UTF-8.") from causa
        except OSError as causa:
            raise PersistenciaError("No se pudo leer una exportación de WhatsApp.") from causa


def _interpretar_chat(valor: object, base: Path) -> _Chat:
    if not isinstance(valor, dict):
        raise EstructuraError("Cada chat debe ser un objeto del manifiesto.")
    entrada = cast(Objeto, valor)
    if (
        not {"archivo", "nombre_crudo", "es_grupo", "remitentes"}
        <= entrada.keys()
        <= {
            "archivo",
            "nombre_crudo",
            "es_grupo",
            "remitentes",
            "telefono",
        }
    ):
        raise EstructuraError("Un chat tiene campos ausentes o desconocidos.")
    archivo, nombre, grupo = entrada["archivo"], entrada["nombre_crudo"], entrada["es_grupo"]
    telefono = entrada.get("telefono")
    remitentes = entrada["remitentes"]
    if (
        not isinstance(archivo, str)
        or not archivo.strip()
        or "\x00" in archivo
        or not isinstance(nombre, str)
        or type(grupo) is not bool
        or not (telefono is None or isinstance(telefono, str))
        or not isinstance(remitentes, list)
        or not remitentes
        or any(not isinstance(remitente, str) for remitente in remitentes)
    ):
        raise EstructuraError("Los tipos de los campos de un chat no son válidos.")
    # Valida identidad antes de abrir archivos, sin inventar nombres o teléfonos.
    if (telefono is not None and not telefono.strip()) or (not nombre.strip() and telefono is None):
        raise ValidacionError("Cada chat requiere un nombre o teléfono no vacío.")
    nombres = cast(list[str], remitentes)
    if len(set(nombres)) != len(nombres) or any(
        not remitente
        or remitente != remitente.strip()
        or "\n" in remitente
        or "\r" in remitente
        or ": " in remitente
        for remitente in nombres
    ):
        raise ValidacionError("Los remitentes deben ser etiquetas no vacías y únicas.")
    relativa = Path(archivo)
    if (
        relativa.is_absolute()
        or relativa.drive
        or any(":" in parte for parte in relativa.parts)
        or ".." in relativa.parts
        or relativa.suffix.lower() != ".txt"
    ):
        raise EstructuraError("Los archivos de chat deben ser rutas relativas TXT sin ascensos.")
    ruta = (base / relativa).resolve()
    if not ruta.is_relative_to(base) or ruta.suffix.lower() != ".txt":
        raise EstructuraError("Una exportación resuelve fuera de la carpeta del manifiesto.")
    return _Chat(
        ruta=ruta,
        nombre=nombre,
        telefono=telefono,
        es_grupo=grupo,
        remitentes=tuple(cast(list[str], remitentes)),
    )


def _leer_manifiesto(ruta: Path) -> Objeto:
    try:
        with ruta.open("rb") as archivo:
            contenido = archivo.read(1024 * 1024 + 1)
        if len(contenido) > 1024 * 1024:
            raise EstructuraError("El manifiesto excede el límite de un MiB.")
        documento: object = json.loads(
            contenido.decode("utf-8-sig"), object_pairs_hook=_sin_duplicados
        )
    except (ValueError, RecursionError) as causa:
        raise EstructuraError("El manifiesto debe ser JSON UTF-8 válido.") from causa
    except OSError as causa:
        raise PersistenciaError("No se pudo leer el manifiesto de importación.") from causa
    if not isinstance(documento, dict):
        raise EstructuraError("El manifiesto debe ser un objeto JSON.")
    return cast(Objeto, documento)


def _sin_duplicados(pares: list[tuple[str, object]]) -> Objeto:
    resultado: Objeto = {}
    for clave, valor in pares:
        if clave in resultado:
            raise ValueError("El manifiesto contiene claves duplicadas.")
        resultado[clave] = valor
    return resultado


def _huella(ruta: Path) -> Huella:
    try:
        estado = ruta.stat()
        if not S_ISREG(estado.st_mode):
            raise EstructuraError("Las entradas de importación deben ser archivos regulares.")
        return estado.st_dev, estado.st_ino, estado.st_size, estado.st_mtime_ns, estado.st_ctime_ns
    except OSError as causa:
        raise PersistenciaError("No se pudo consultar una entrada de importación.") from causa


def _verificar_huella(ruta: Path, esperada: Huella) -> None:
    try:
        actual = _huella(ruta)
    except (PersistenciaError, EstructuraError) as causa:
        raise ConcurrenciaError("Una entrada cambió durante la importación.") from causa
    if actual != esperada:
        raise ConcurrenciaError("Una entrada cambió durante la importación.")
