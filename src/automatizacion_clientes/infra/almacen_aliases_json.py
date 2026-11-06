"""Aliases tipados compatibles con el formato JSON histórico del proyecto."""

import logging
from pathlib import Path

from automatizacion_clientes.core.aliases import conciliar_alias
from automatizacion_clientes.core.errores import (
    ConcurrenciaError,
    EstructuraError,
    PersistenciaError,
)
from automatizacion_clientes.core.modelos import AliasContacto, DestinoAlias
from automatizacion_clientes.infra.almacenamiento_json import ValorJson, guardar_json, leer_json

LOGGER = logging.getLogger(__name__)


class AlmacenAliasesJson:
    """Mantiene una sesión de aliases y los persiste solo al llamar a guardar.

    Satisface estructuralmente ``AlmacenAliases``. Conserva nombres y teléfonos
    exactos, sin normalizarlos, y admite números compartidos. La consulta por
    teléfono devuelve todos sus aliases en orden por nombre original.

    Un archivo ausente inicia una sesión vacía sin crear carpetas. Los archivos
    corruptos o con campos desconocidos producen un error; no se descartan.
    Las referencias a personas y hojas se validan contra el catálogo en el motor.

    Antes de guardar se comprueba el estado persistido contra la instantánea
    cargada. Esto detecta cambios previos, pero no bloquea otros procesos durante
    el intervalo entre comprobación y reemplazo. Debe haber un único escritor.
    """

    def __init__(self, ruta: Path) -> None:
        """Carga y valida los aliases existentes sin modificar el archivo.

        Args:
            ruta: Archivo de aliases, normalmente obtenido de ``Config``.

        Raises:
            EstructuraError: El documento no respeta el esquema de aliases.
            ValidacionError: Una identidad o sus destinos incumplen el dominio.
            PersistenciaError: No se puede leer por una causa distinta de ausencia.
        """
        self._ruta = ruta
        self._persistidos = _cargar_aliases(ruta)
        self._aliases: dict[str, AliasContacto] = (
            {} if self._persistidos is None else self._persistidos.copy()
        )

    def buscar_por_nombre(self, nombre_crudo: str) -> AliasContacto | None:
        """Busca el nombre original exacto sin modificar la sesión ni el disco."""
        return self._aliases.get(nombre_crudo)

    def __len__(self) -> int:
        """Devuelve la cantidad de asociaciones de la sesión, sin consultar archivos."""
        return len(self._aliases)

    def buscar_por_telefono(self, telefono: str) -> tuple[AliasContacto, ...]:
        """Devuelve todos los aliases del teléfono exacto en orden estable."""
        return tuple(
            self._aliases[nombre]
            for nombre in sorted(self._aliases)
            if self._aliases[nombre].telefono == telefono
        )

    def registrar(self, alias: AliasContacto) -> str | None:
        """Acepta un alias o devuelve un aviso de conflicto sin datos personales.

        Los destinos se comparan como conjuntos: reordenar personas u hojas
        no crea conflictos. Una entrada compatible puede completar el teléfono
        ausente; omitirlo no borra el teléfono ya conocido. Los cambios se
        mantienen en memoria hasta ``guardar``.
        """
        conservado, aviso = conciliar_alias(self._aliases.get(alias.nombre_crudo), alias)
        self._aliases[alias.nombre_crudo] = conservado
        if aviso is not None:
            LOGGER.warning(aviso)
        return aviso

    def guardar(self) -> None:
        """Guarda atómicamente tras comprobar que el archivo no cambió.

        Los cambios aceptados permanecen en memoria si falla el guardado. Tras
        un conflicto externo, se debe abrir otra sesión y revisar esos cambios.
        No se fusionan silenciosamente asociaciones de otros escritores.

        Raises:
            ConcurrenciaError: El estado persistido difiere de la instantánea.
            EstructuraError: El archivo actual está corrupto o tiene otro esquema.
            ValidacionError: El archivo actual contiene identidades inválidas.
            PersistenciaError: Falló la lectura, escritura o sustitución del archivo.
        """
        if _cargar_aliases(self._ruta) != self._persistidos:
            raise ConcurrenciaError(
                "El archivo de aliases cambió. Abra otra sesión y revise los cambios pendientes."
            )

        documento: dict[str, ValorJson] = {}
        for nombre, alias in self._aliases.items():
            personas: list[ValorJson] = []
            for destino in alias.personas:
                hojas: list[ValorJson] = list(sorted(destino.hojas))
                personas.append({"persona_id": destino.persona_id, "hojas": hojas})
            documento[nombre] = {"personas": personas, "telefono": alias.telefono}

        guardar_json(self._ruta, documento)
        self._persistidos = self._aliases.copy()


def _cargar_aliases(ruta: Path) -> dict[str, AliasContacto] | None:
    try:
        documento = leer_json(ruta)
    except PersistenciaError as causa:
        if isinstance(causa.__cause__, FileNotFoundError):
            return None
        raise

    if not isinstance(documento, dict):
        raise EstructuraError("El documento de aliases debe ser un objeto JSON.")
    return {nombre: _decodificar_alias(nombre, entrada) for nombre, entrada in documento.items()}


def _decodificar_alias(nombre: str, entrada: ValorJson) -> AliasContacto:
    if not isinstance(entrada, dict) or not {"personas"} <= entrada.keys() <= {
        "personas",
        "telefono",
    }:
        raise EstructuraError("La entrada del alias contiene campos ausentes o desconocidos.")
    personas = entrada["personas"]
    telefono = entrada.get("telefono")
    if not isinstance(personas, list) or not (telefono is None or isinstance(telefono, str)):
        raise EstructuraError("Los tipos de la entrada del alias no son válidos.")
    return AliasContacto(
        nombre_crudo=nombre,
        telefono=telefono,
        personas=tuple(_decodificar_destino(persona) for persona in personas),
    )


def _decodificar_destino(entrada: ValorJson) -> DestinoAlias:
    if not isinstance(entrada, dict) or entrada.keys() != {"persona_id", "hojas"}:
        raise EstructuraError("El destino del alias contiene campos ausentes o desconocidos.")
    persona_id = entrada["persona_id"]
    hojas = entrada["hojas"]
    if not isinstance(persona_id, str) or not isinstance(hojas, list):
        raise EstructuraError("Los tipos del destino del alias no son válidos.")
    hojas_validadas: set[str] = set()
    for hoja in hojas:
        if not isinstance(hoja, str) or hoja in hojas_validadas:
            raise EstructuraError("Las hojas del alias deben ser cadenas sin duplicados.")
        hojas_validadas.add(hoja)
    return DestinoAlias(persona_id=persona_id, hojas=frozenset(hojas_validadas))
