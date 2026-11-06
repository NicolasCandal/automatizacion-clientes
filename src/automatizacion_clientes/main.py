"""Comando de importación WhatsApp y registro en Excel o Google Sheets."""

import argparse
import logging
from collections.abc import Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import NoReturn

from pydantic import ValidationError
from pydantic_settings import SettingsError

from automatizacion_clientes.config import Config, cargar_config
from automatizacion_clientes.core.contratos import EscritorClientes, LectorRegistroClientes, Reloj
from automatizacion_clientes.core.errores import (
    AutenticacionError,
    AutomatizacionError,
    AutorizacionError,
    ConcurrenciaError,
    ConfiguracionError,
    DominioError,
    InfraestructuraError,
    RegistroInterrumpidoError,
)
from automatizacion_clientes.core.modelos import PeriodoSemanal
from automatizacion_clientes.infra.almacen_aliases_json import AlmacenAliasesJson
from automatizacion_clientes.infra.almacen_catalogo_json import AlmacenCatalogoJson
from automatizacion_clientes.infra.almacen_resoluciones_json import AlmacenResolucionesJson
from automatizacion_clientes.infra.almacen_semana_contactos_json import AlmacenSemanaContactosJson
from automatizacion_clientes.infra.autenticacion_google import ProveedorCredencialesGoogle
from automatizacion_clientes.infra.comparador_rapidfuzz import ComparadorRapidFuzz
from automatizacion_clientes.infra.escritor_clientes_excel import EscritorClientesExcel
from automatizacion_clientes.infra.escritor_clientes_sheets import (
    ALCANCES_ESCRITURA,
    crear_escritor_sheets,
)
from automatizacion_clientes.infra.extractor_whatsapp_txt import ExtractorWhatsAppTxt
from automatizacion_clientes.infra.lector_clientes_excel import LectorClientesExcel
from automatizacion_clientes.infra.lector_clientes_sheets import crear_lector_sheets
from automatizacion_clientes.infra.lector_contactos_json import LectorContactosJson
from automatizacion_clientes.infra.lista_exclusion_json import ListaExclusionJson
from automatizacion_clientes.infra.logging_consola import logging_consola
from automatizacion_clientes.infra.reloj_sistema import RelojSistema
from automatizacion_clientes.services.automatizacion import (
    ResultadoAutomatizacion,
    ServicioAutomatizacion,
)
from automatizacion_clientes.services.catalogo import ServicioCatalogo
from automatizacion_clientes.services.coincidencias import ServicioCoincidencias
from automatizacion_clientes.services.importacion_whatsapp import (
    ResultadoImportacionWhatsApp,
    ServicioImportacionWhatsApp,
)
from automatizacion_clientes.services.registro import ServicioRegistro

LOGGER = logging.getLogger(__name__)


@dataclass
class _Opciones(argparse.Namespace):
    encabezado: str = ""
    archivo_env: Path | None = None
    sin_env: bool = False
    directorio_base: Path | None = None
    simulacion: bool | None = None
    permitir_pendientes: bool = False
    reconstruir_catalogo: bool = False
    ayuda: bool = False
    autorizar_google: bool = False
    importar_whatsapp: bool = False
    desde: date | None = None
    hasta: date | None = None
    manifiesto: Path | None = None


class _EscritorSimulado:
    """Puerto sin acceso a recursos que impide escrituras en una simulación Sheets."""

    def crear_respaldo(self) -> Path:
        raise ConfiguracionError("Una simulación no puede crear respaldos.")

    def aplicar_marcas(
        self,
        hoja: str,
        columna: int,
        encabezado: str,
        filas: frozenset[int],
        *,
        revision_esperada: str,
    ) -> None:
        raise ConfiguracionError("Una simulación no puede aplicar marcas.")


class _Analizador(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        """Traduce errores sin imprimir ni exponer argumentos desconocidos."""
        raise ConfiguracionError("Argumentos inválidos. Consulte --help.")


def _crear_analizador() -> _Analizador:
    analizador = _Analizador(
        prog="automatizacion-clientes",
        add_help=False,
        allow_abbrev=False,
        exit_on_error=False,
        description="Importa chats TXT de WhatsApp o registra contactos en Excel o Google Sheets.",
    )
    analizador.add_argument(
        "-h",
        "--help",
        dest="ayuda",
        action="store_true",
        help="Mostrar esta ayuda sin cargar archivos.",
    )
    operacion = analizador.add_mutually_exclusive_group()
    operacion.add_argument(
        "--autorizar-google",
        action="store_true",
        help="Autorizar OAuth en el navegador y guardar el token; sin ejecutar el registro.",
    )
    operacion.add_argument(
        "--importar-whatsapp",
        action="store_true",
        help="Generar la entrada semanal desde TXT; sin ejecutar el registro de marcas.",
    )
    analizador.add_argument(
        "--desde",
        type=_fecha_argumento,
        help="Inicio inclusivo YYYY-MM-DD; obligatorio al importar WhatsApp.",
    )
    analizador.add_argument(
        "--hasta",
        type=_fecha_argumento,
        help="Fin inclusivo YYYY-MM-DD; obligatorio al importar WhatsApp.",
    )
    analizador.add_argument(
        "--manifiesto",
        type=Path,
        help="Manifiesto de importación; prevalece sobre AC_WHATSAPP_MANIFIESTO_PATH.",
    )
    analizador.add_argument(
        "--encabezado",
        default="",
        help="Identificador de la semana; use una etiqueta con año o la histórica exacta.",
    )
    entorno = analizador.add_mutually_exclusive_group()
    entorno.add_argument(
        "--env-file",
        dest="archivo_env",
        type=Path,
        default=None,
        help="Archivo de configuración; por defecto .env.",
    )
    entorno.add_argument(
        "--sin-env",
        action="store_true",
        help="Usar solo variables de entorno y valores predeterminados.",
    )
    analizador.add_argument(
        "--directorio-base",
        type=Path,
        help="Base para resolver la configuración y las rutas relativas.",
    )
    modo = analizador.add_mutually_exclusive_group()
    modo.add_argument(
        "--simular",
        dest="simulacion",
        action="store_const",
        const=True,
        default=None,
        help="Recorrer todo el flujo sin guardar archivos.",
    )
    modo.add_argument(
        "--aplicar",
        dest="simulacion",
        action="store_const",
        const=False,
        help="Guardar la entrada importada o registrar marcas con respaldo, según la operación.",
    )
    analizador.add_argument(
        "--permitir-pendientes",
        action="store_true",
        help="Permitir un reemplazo parcial usando solo los resultados resueltos.",
    )
    analizador.add_argument(
        "--reconstruir-catalogo",
        action="store_true",
        help="Reconstruir el catálogo, incluso si su caché tiene formato inválido.",
    )
    return analizador


def _fecha_argumento(valor: str) -> date:
    """Exige fechas canónicas antes de cargar configuración o abrir entradas."""
    try:
        fecha = date.fromisoformat(valor)
    except ValueError as causa:
        raise argparse.ArgumentTypeError("Use una fecha válida YYYY-MM-DD.") from causa
    if fecha.isoformat() != valor:
        raise argparse.ArgumentTypeError("Use exactamente el formato YYYY-MM-DD.")
    return fecha


def _validar_opciones(opciones: _Opciones) -> PeriodoSemanal | None:
    """Separa autorización, importación y registro sin acceder a recursos."""
    if not opciones.importar_whatsapp and (
        opciones.desde is not None or opciones.hasta is not None or opciones.manifiesto is not None
    ):
        raise ConfiguracionError("Las fechas y el manifiesto requieren --importar-whatsapp.")
    if opciones.autorizar_google:
        if (
            opciones.encabezado
            or opciones.simulacion is not None
            or opciones.permitir_pendientes
            or opciones.reconstruir_catalogo
        ):
            raise ConfiguracionError("La autorización de Google no admite opciones de registro.")
        return None
    if opciones.importar_whatsapp:
        if opciones.encabezado or opciones.permitir_pendientes or opciones.reconstruir_catalogo:
            raise ConfiguracionError("La importación no admite opciones de registro.")
        if opciones.desde is None or opciones.hasta is None:
            raise ConfiguracionError("La importación requiere --desde y --hasta.")
        return PeriodoSemanal(desde=opciones.desde, hasta=opciones.hasta)
    if not opciones.encabezado.strip():
        raise ConfiguracionError("Indique --encabezado con la semana que desea registrar.")
    return None


def validar_rutas(configuracion: Config, *, archivo_env: Path | None = None) -> None:
    """Rechaza archivos coincidentes o anidados antes de abrir adaptadores."""
    archivos: tuple[Path, ...] = (
        configuracion.excel_path,
        configuracion.contactos_path,
        configuracion.whatsapp_manifiesto_path,
        configuracion.catalogo_path,
        configuracion.aliases_path,
        configuracion.lista_negra_path,
        configuracion.resolucion_path,
        configuracion.credentials_path,
        configuracion.token_path,
    )
    if archivo_env is not None:
        archivos = (*archivos, archivo_env.expanduser().resolve())
    for indice, ruta in enumerate(archivos):
        if any(
            ruta.is_relative_to(otra) or otra.is_relative_to(ruta)
            for otra in archivos[indice + 1 :]
        ):
            raise ConfiguracionError(
                "Las rutas de archivos deben ser distintas y no estar anidadas."
            )
        if configuracion.directorio_respaldos.is_relative_to(ruta):
            raise ConfiguracionError("La carpeta de respaldos no puede ocupar una ruta de archivo.")


def ejecutar_excel(
    configuracion: Config,
    *,
    encabezado: str,
    simulacion: bool | None = None,
    permitir_pendientes: bool = False,
    forzar_catalogo: bool = False,
) -> ResultadoAutomatizacion:
    """Construye adaptadores, ejecuta los servicios y cierra el lector de Excel.

    Args:
        configuracion: Opciones validadas con rutas ``Path``.
        encabezado: Identificador explícito del período a registrar.
        simulacion: Sustituye ``Config.simulacion`` si se proporciona.
        permitir_pendientes: Permite registrar únicamente la parte resuelta.
        forzar_catalogo: Permite reconstruir una caché de formato inválido.

    Returns:
        Resolución y registro del flujo, sin lanzar procesos ni contactar Google.

    Raises:
        ConfiguracionError: Rutas incompatibles o repositorio distinto de Excel.
        AutomatizacionError: Fallo del flujo; puede haber archivos auxiliares guardados.
    """
    if configuracion.repositorio != "excel":
        raise ConfiguracionError("Este punto de entrada requiere el repositorio Excel.")
    if not encabezado.strip():
        raise ConfiguracionError("Indique el encabezado de la semana.")
    validar_rutas(configuracion)
    reloj = RelojSistema()
    with LectorClientesExcel(configuracion.excel_path) as lector:
        return _ejecutar_con_adaptadores(
            configuracion,
            lector=lector,
            escritor=EscritorClientesExcel(
                configuracion.excel_path,
                directorio_respaldos=configuracion.directorio_respaldos,
                reloj=reloj,
            ),
            reloj=reloj,
            origen=configuracion.excel_path.as_uri(),
            encabezado=encabezado,
            simulacion=configuracion.simulacion if simulacion is None else simulacion,
            permitir_pendientes=permitir_pendientes,
            forzar_catalogo=forzar_catalogo,
        )


def ejecutar_sheets(
    configuracion: Config,
    *,
    encabezado: str,
    simulacion: bool | None = None,
    permitir_pendientes: bool = False,
    forzar_catalogo: bool = False,
) -> ResultadoAutomatizacion:
    """Conecta Google al flujo compartido y cierra sus sesiones incluso ante fallos.

    En simulación carga y renueva credenciales en memoria, no guarda el token
    ni construye un escritor autorizado. El modo real exige alcances de escritura
    explícitos y puede guardar el token renovado antes de resolver los contactos.
    Nunca inicia consentimiento ni abre el navegador automáticamente.

    Args:
        configuracion: Configuración con repositorio Sheets e identificador remoto.
        encabezado: Identificador explícito del período a registrar.
        simulacion: Sustituye el modo configurado si se proporciona.
        permitir_pendientes: Autoriza registrar únicamente los contactos resueltos.
        forzar_catalogo: Permite reconstruir una caché con formato inválido.

    Returns:
        Resolución y registro, o resolución sin registro cuando hay pendientes.

    Raises:
        ConfiguracionError: Rutas, repositorio, encabezado o alcances incompatibles.
        AutomatizacionError: Falló la autenticación, la lectura o alguna fase del flujo.
    """
    if configuracion.repositorio != "sheets" or configuracion.spreadsheet_id is None:
        raise ConfiguracionError("Este punto de entrada requiere un documento de Google Sheets.")
    if not encabezado.strip():
        raise ConfiguracionError("Indique el encabezado de la semana.")
    validar_rutas(configuracion)
    simular = configuracion.simulacion if simulacion is None else simulacion
    if not simular and not ALCANCES_ESCRITURA.intersection(configuracion.google_alcances):
        raise ConfiguracionError("Configure un alcance de escritura antes de aplicar en Sheets.")
    credenciales = ProveedorCredencialesGoogle(configuracion).cargar(persistir=not simular)
    reloj = RelojSistema()
    origen = f"sheets:{configuracion.spreadsheet_id}"
    with ExitStack() as recursos:
        lector = recursos.enter_context(crear_lector_sheets(configuracion, credenciales))
        escritor: EscritorClientes = _EscritorSimulado()
        if not simular:
            escritor = recursos.enter_context(
                crear_escritor_sheets(configuracion, credenciales, reloj=reloj)
            )
        return _ejecutar_con_adaptadores(
            configuracion,
            lector=lector,
            escritor=escritor,
            reloj=reloj,
            origen=origen,
            encabezado=encabezado,
            simulacion=simular,
            permitir_pendientes=permitir_pendientes,
            forzar_catalogo=forzar_catalogo,
        )


def autorizar_google(configuracion: Config) -> None:
    """Inicia consentimiento OAuth explícito y guarda el token, sin leer contactos."""
    if configuracion.repositorio != "sheets":
        raise ConfiguracionError("La autorización de Google requiere el repositorio Sheets.")
    validar_rutas(configuracion)
    ProveedorCredencialesGoogle(configuracion).autorizar()
    LOGGER.info("Google autorizado; el registro de contactos no se ha ejecutado.")


def ejecutar_importacion(
    configuracion: Config,
    *,
    periodo: PeriodoSemanal,
    simulacion: bool | None = None,
) -> ResultadoImportacionWhatsApp:
    """Compone la importación local y genera únicamente la entrada semanal.

    Args:
        configuracion: Configuración validada; las rutas deben ser independientes.
        periodo: Fechas explícitas e inclusivas de los contactos a extraer.
        simulacion: Sustituye Config.simulacion; simular evita todos los guardados.

    Returns:
        Lote validado y modo de ejecución, sin registrar marcas o abrir Google.

    Raises:
        ConfiguracionError: Rutas o destino incompatibles con la importación.
        AutomatizacionError: Falló la lectura, validación o guardado de la semana.
    """
    validar_rutas(configuracion)
    servicio = ServicioImportacionWhatsApp(
        extractor=ExtractorWhatsAppTxt(configuracion.whatsapp_manifiesto_path),
        almacen=AlmacenSemanaContactosJson(
            configuracion.contactos_path,
            manifiesto=configuracion.whatsapp_manifiesto_path,
        ),
        reloj=RelojSistema(),
    )
    return servicio.ejecutar(
        periodo,
        simulacion=configuracion.simulacion if simulacion is None else simulacion,
    )


def _ejecutar_con_adaptadores(
    configuracion: Config,
    *,
    lector: LectorRegistroClientes,
    escritor: EscritorClientes,
    reloj: Reloj,
    origen: str,
    encabezado: str,
    simulacion: bool,
    permitir_pendientes: bool,
    forzar_catalogo: bool,
) -> ResultadoAutomatizacion:
    """Compone los mismos servicios de negocio para cualquiera de los dos orígenes."""
    catalogos = AlmacenCatalogoJson(configuracion.catalogo_path)
    informes = AlmacenResolucionesJson(configuracion.resolucion_path)
    contactos = LectorContactosJson(configuracion.contactos_path)
    flujo = ServicioAutomatizacion(
        catalogo=ServicioCatalogo(
            lector_clientes=lector,
            almacen_catalogo=catalogos,
            reloj=reloj,
            identificador_origen=origen,
        ),
        coincidencias=ServicioCoincidencias(
            lector_contactos=contactos,
            almacen_catalogo=catalogos,
            almacen_aliases=AlmacenAliasesJson(configuracion.aliases_path),
            lista_exclusion=ListaExclusionJson(configuracion.lista_negra_path),
            almacen_resoluciones=informes,
            comparador=ComparadorRapidFuzz(),
            politica=configuracion.crear_politica_coincidencia(),
        ),
        registro=ServicioRegistro(
            lector_clientes=lector,
            escritor_clientes=escritor,
            identificador_origen=origen,
        ),
        lector_contactos=contactos,
        almacen_catalogo=catalogos,
        almacen_resoluciones=informes,
    )
    return flujo.ejecutar(
        encabezado=encabezado,
        simulacion=simulacion,
        permitir_pendientes=permitir_pendientes,
        forzar_catalogo=forzar_catalogo,
    )


def main(argumentos: Sequence[str] | None = None) -> int:
    """Ejecuta el comando y devuelve un código de salida, sin imprimir secretos.

    Códigos: 0 completado, 1 inesperado, 2 entrada/configuración, 3 pendientes,
    4 infraestructura, 5 concurrencia, 6 registro interrumpido, 130 interrupción.
    La ayuda y los diagnósticos usan logging en stderr; importar no ejecuta nada.
    """
    with logging_consola() as paquete:
        try:
            analizador = _crear_analizador()
            opciones = _Opciones()
            analizador.parse_args(argumentos, namespace=opciones)
            if opciones.ayuda:
                LOGGER.info("%s", analizador.format_help().rstrip())
                return 0
            periodo = _validar_opciones(opciones)
            ruta_env = None if opciones.sin_env else (opciones.archivo_env or Path(".env"))
            ruta_env_resuelta = ruta_env.expanduser() if ruta_env is not None else None
            if ruta_env_resuelta is not None and opciones.directorio_base is not None:
                ruta_env_resuelta = (
                    opciones.directorio_base.expanduser().resolve() / ruta_env_resuelta
                )
            if opciones.archivo_env is not None and (
                ruta_env_resuelta is None or not ruta_env_resuelta.is_file()
            ):
                raise ConfiguracionError("El archivo indicado con --env-file debe existir.")
            configuracion = cargar_config(ruta_env, directorio_base=opciones.directorio_base)
            if opciones.manifiesto is not None:
                manifiesto = opciones.manifiesto.expanduser()
                if not manifiesto.is_absolute():
                    manifiesto = configuracion.directorio_base / manifiesto
                configuracion = configuracion.model_copy(
                    update={"whatsapp_manifiesto_path": manifiesto.resolve()}
                )
            validar_rutas(configuracion, archivo_env=ruta_env_resuelta)
            paquete.setLevel(configuracion.log_level)
            if periodo is not None:
                importacion = ejecutar_importacion(
                    configuracion, periodo=periodo, simulacion=opciones.simulacion
                )
                LOGGER.info(
                    "Entrada semanal preparada: simulación=%s; %s chats; %s contactos.",
                    importacion.simulacion,
                    importacion.extraccion.total_chats_escaneados,
                    len(importacion.extraccion.semana.contactos),
                )
                return 0
            if opciones.autorizar_google:
                autorizar_google(configuracion)
                return 0
            ejecutar = ejecutar_excel if configuracion.repositorio == "excel" else ejecutar_sheets
            resultado = ejecutar(
                configuracion,
                encabezado=opciones.encabezado.strip(),
                simulacion=opciones.simulacion,
                permitir_pendientes=opciones.permitir_pendientes,
                forzar_catalogo=opciones.reconstruir_catalogo,
            )
            if resultado.registro is None:
                return 3
            LOGGER.info(
                "Flujo completado: simulación=%s; %s hojas aplicadas.",
                resultado.simulacion,
                len(resultado.registro.hojas_aplicadas),
            )
            return 0
        except (argparse.ArgumentError, ValidationError, SettingsError):
            LOGGER.error("[CONFIGURACION] Revise los argumentos y las variables AC_.")
            return 2
        except RegistroInterrumpidoError as causa:
            LOGGER.error(
                "[%s] Registro detenido: %s hojas confirmadas; hoja en curso=%s. "
                "Revise el respaldo configurado.",
                causa.CODIGO,
                len(causa.hojas_confirmadas),
                causa.hoja_en_curso is not None,
            )
            return 6
        except ConcurrenciaError:
            LOGGER.error(
                "[CONCURRENCIA] El origen cambió. Actualice las entradas antes de repetir."
            )
            return 5
        except (ConfiguracionError, DominioError) as causa:
            LOGGER.error(
                "[%s] La entrada no permite completar el flujo. "
                "Consulte --help y revise la configuración.",
                causa.CODIGO,
            )
            return 2
        except (AutenticacionError, AutorizacionError) as causa:
            LOGGER.error(
                "[%s] Revise las credenciales y permisos de Google. "
                "Para autorizar OAuth use --autorizar-google como operación separada.",
                causa.CODIGO,
            )
            return 4
        except (InfraestructuraError, OSError):
            LOGGER.error(
                "[INFRAESTRUCTURA] No se pudo acceder o guardar un recurso. "
                "Puede haber archivos auxiliares ya guardados."
            )
            return 4
        except AutomatizacionError as causa:
            LOGGER.error(
                "[%s] El flujo no pudo completarse. "
                "Revise los archivos auxiliares antes de repetir.",
                causa.CODIGO,
            )
            return 1
        except KeyboardInterrupt:
            LOGGER.warning(
                "Ejecución interrumpida. Revise el origen y los respaldos antes de continuar."
            )
            return 130
        except Exception:
            LOGGER.error(
                "[INESPERADO] El flujo se detuvo. "
                "Revise el estado y los respaldos antes de repetir."
            )
            return 1
