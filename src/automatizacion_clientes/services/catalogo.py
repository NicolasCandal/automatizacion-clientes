"""Lectura coherente del origen y gestión explícita de la caché de clientes."""

import logging

from automatizacion_clientes.core.catalogo import construir_catalogo
from automatizacion_clientes.core.contratos import AlmacenCatalogo, LectorClientes, Reloj
from automatizacion_clientes.core.errores import (
    ConcurrenciaError,
    ConfiguracionError,
    EstructuraError,
    ValidacionError,
)
from automatizacion_clientes.core.modelos import CatalogoClientes

LOGGER = logging.getLogger(__name__)


class ServicioCatalogo:
    """Obtiene una instantánea coherente sin permitir escrituras en la planilla.

    Reutiliza una caché del mismo origen y revisión, o lee todas las hojas y
    construye un catálogo con IDs estables. Las revisiones detectan cambios,
    pero no bloquean el origen ni garantizan una transacción con la caché.
    El propietario del lector se encarga de cerrar sus archivos o conexiones.
    """

    def __init__(
        self,
        *,
        lector_clientes: LectorClientes,
        almacen_catalogo: AlmacenCatalogo,
        reloj: Reloj,
        identificador_origen: str,
    ) -> None:
        """Recibe dependencias sin ejecutar ninguna operación externa.

        Args:
            lector_clientes: Acceso de solo lectura a estructura, clientes y revisión.
            almacen_catalogo: Caché local que conserva identidades anteriores.
            reloj: Fuente de instantes con zona horaria para catálogos nuevos.
            identificador_origen: Identidad estable de la planilla; debe conservarse.

        Raises:
            ConfiguracionError: El identificador del origen está vacío.
        """
        if not identificador_origen.strip():
            raise ConfiguracionError("Se necesita un identificador estable del origen.")
        self._lector = lector_clientes
        self._almacen = almacen_catalogo
        self._reloj = reloj
        self._identificador_origen = identificador_origen

    def ejecutar(self, *, simulacion: bool = True, forzar: bool = False) -> CatalogoClientes:
        """Reutiliza o reconstruye la caché; simula sin guardar por defecto.

        Una reconstrucción forzada conserva los IDs de una caché válida. Si
        esta es ilegible por formato, solo ``forzar=True`` permite reconstruirla;
        los errores de acceso nunca se ocultan. No regenera aliases ni los migra.

        Args:
            simulacion: Devuelve el catálogo sin escribir la caché.
            forzar: Reconstruye incluso con igual revisión y permite reparar formato.

        Returns:
            Catálogo reutilizado o construido a partir de la instantánea leída.

        Raises:
            ConfiguracionError: La caché válida pertenece a otro origen.
            ConcurrenciaError: El origen cambió durante la operación.
            EstructuraError: El origen o la caché tienen una estructura incorrecta.
            ValidacionError: Los modelos, filas, titulares o fechas son inválidos.
            InfraestructuraError: Falló una lectura o la persistencia de la caché.
        """
        anterior = self._leer_cache(forzar=forzar)
        if anterior is not None and anterior.identificador_origen != self._identificador_origen:
            raise ConfiguracionError(
                "La caché pertenece a otro origen. Configure otra ruta de catálogo."
            )
        revision = self._lector.revision()
        if not revision.strip():
            raise EstructuraError("El origen debe devolver una revisión no vacía.")
        if anterior is not None and anterior.revision == revision and not forzar:
            self._comprobar_revision(revision)
            LOGGER.info("Caché de catálogo reutilizada: %s personas.", len(anterior.personas))
            return anterior
        hojas = self._lector.listar_hojas()
        clientes_por_hoja = {hoja.nombre: self._lector.leer_clientes(hoja.nombre) for hoja in hojas}
        self._comprobar_revision(revision)
        catalogo = construir_catalogo(
            identificador_origen=self._identificador_origen,
            revision=revision,
            generado_en=self._reloj.ahora(),
            hojas=hojas,
            clientes_por_hoja=clientes_por_hoja,
            anterior=anterior,
        )
        self._comprobar_revision(revision)
        if not simulacion:
            self._almacen.guardar(catalogo)
        LOGGER.info(
            "Catálogo construido: simulación=%s; %s personas y %s hojas.",
            simulacion,
            len(catalogo.personas),
            len(catalogo.hojas),
        )
        return catalogo

    def _leer_cache(self, *, forzar: bool) -> CatalogoClientes | None:
        try:
            return self._almacen.leer()
        except (EstructuraError, ValidacionError):
            if not forzar:
                raise
            LOGGER.warning(
                "La caché no es válida; se reconstruirá sin recuperar sus IDs anteriores."
            )
            return None

    def _comprobar_revision(self, revision_esperada: str) -> None:
        if self._lector.revision() != revision_esperada:
            raise ConcurrenciaError(
                "El origen cambió durante la construcción del catálogo. Vuelva a leerlo."
            )
