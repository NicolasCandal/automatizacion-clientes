"""Decisión de coincidencias sin archivos, red ni persistencia de aliases."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from .brokers import TABLA_BROKERS_PREDETERMINADA, extraer_brokers_y_nombre
from .contratos import ComparadorNombres
from .errores import ValidacionError
from .modelos import (
    AliasContacto,
    CandidatoCoincidencia,
    CatalogoClientes,
    ContactoIgnorado,
    ContactoPendiente,
    ContactoResuelto,
    ContactoSemanal,
    MotivoIgnorado,
    MotivoPendiente,
    PersonaCliente,
    PoliticaCoincidencia,
    ResultadoContacto,
    ViaCoincidencia,
)
from .normalizacion import clave_orden, extraer_titulares, normalizar


def _crear_candidato(
    persona: PersonaCliente, puntuacion: float, via: ViaCoincidencia
) -> CandidatoCoincidencia:
    """Valida la puntuación antes de ordenar o descartar una propuesta."""
    return CandidatoCoincidencia(
        persona_id=persona.persona_id,
        nombre_canonico=persona.nombre_canonico,
        hojas=persona.hojas,
        puntuacion=puntuacion,
        via=via,
    )


class MotorCoincidencias:
    """Resuelve contactos contra una instantánea de clientes y un comparador puro.

    Los servicios consultan aliases y exclusiones, y entregan sus resultados
    como valores. El motor no consulta almacenes ni aprende o guarda aliases.
    La prioridad es grupo/exclusión, teléfono, alias por nombre, exacto y fuzzy.
    Los brokers restringen los destinos, también al utilizar aliases.
    """

    def __init__(
        self,
        catalogo: CatalogoClientes,
        comparador: ComparadorNombres,
        *,
        politica: PoliticaCoincidencia | None = None,
        tabla_brokers: Mapping[str, str] | None = None,
    ) -> None:
        """Construye los índices una sola vez para la instantánea recibida.

        Args:
            catalogo: Catálogo validado cuyos identificadores usa el motor.
            comparador: Comparación de nombres normalizados, sin efectos externos.
            politica: Umbrales y límite de sugerencias; usa los valores del dominio.
            tabla_brokers: Tabla opcional que reemplaza los alias predeterminados.
        """
        self._personas: Mapping[str, PersonaCliente] = MappingProxyType(
            {persona.persona_id: persona for persona in catalogo.personas}
        )
        self._indice_exacto: Mapping[str, PersonaCliente] = MappingProxyType(
            {persona.clave_orden: persona for persona in catalogo.personas}
        )
        self._nombres: tuple[tuple[PersonaCliente, tuple[str, ...]], ...] = tuple(
            (persona, tuple(normalizar(titular) for titular in persona.titulares))
            for persona in catalogo.personas
        )
        self._comparador: ComparadorNombres = comparador
        self._politica: PoliticaCoincidencia = politica or PoliticaCoincidencia()
        self._tabla_brokers: Mapping[str, str] = MappingProxyType(
            dict(TABLA_BROKERS_PREDETERMINADA if tabla_brokers is None else tabla_brokers)
        )

    def resolver_contacto(
        self,
        contacto: ContactoSemanal,
        *,
        aliases_telefono: tuple[AliasContacto, ...] = (),
        alias_nombre: AliasContacto | None = None,
        excluido: bool = False,
    ) -> tuple[ResultadoContacto, ...]:
        """Devuelve decisiones por titular sin modificar las entradas.

        Args:
            contacto: Metadatos del chat que se quiere identificar.
            aliases_telefono: Todos los aliases encontrados para su teléfono.
            alias_nombre: Alias encontrado por el nombre original exacto.
            excluido: Resultado de consultar la lista de exclusión en el servicio.

        Returns:
            Resultados inmutables. Un contacto con cotitulares puede producir
            varios resultados; una persona resuelta no se repite.

        Raises:
            ValidacionError: Si la evidencia corresponde a otro contacto o el
                comparador devuelve una puntuación fuera de su contrato.
        """
        if contacto.es_grupo:
            return (ContactoIgnorado(contacto=contacto, motivo=MotivoIgnorado.GRUPO),)
        if excluido:
            return (ContactoIgnorado(contacto=contacto, motivo=MotivoIgnorado.EN_LISTA_NEGRA),)

        nombre = extraer_brokers_y_nombre(contacto.nombre_crudo, self._tabla_brokers)
        hojas = frozenset(nombre.hojas)
        if aliases_telefono:
            if contacto.telefono is None or any(
                alias.telefono != contacto.telefono for alias in aliases_telefono
            ):
                raise ValidacionError(
                    "Los aliases recibidos no corresponden al teléfono del contacto."
                )
            return self._resolver_aliases(
                contacto, aliases_telefono, hojas, ViaCoincidencia.TELEFONO
            )
        if alias_nombre is not None:
            if alias_nombre.nombre_crudo != contacto.nombre_crudo:
                raise ValidacionError("El alias recibido no corresponde al nombre del contacto.")
            return self._resolver_aliases(contacto, (alias_nombre,), hojas, ViaCoincidencia.ALIAS)

        resultados: list[ResultadoContacto] = []
        personas_resueltas: set[str] = set()
        for titular in extraer_titulares(nombre.nombre_sin_parentesis) or (
            nombre.nombre_sin_parentesis,
        ):
            resultado = self._resolver_titular(contacto, titular, hojas)
            if isinstance(resultado, ContactoResuelto):
                if resultado.persona_id in personas_resueltas:
                    continue
                personas_resueltas.add(resultado.persona_id)
            resultados.append(resultado)
        return tuple(resultados)

    def _candidatos_para(self, titular: str) -> tuple[CandidatoCoincidencia, ...]:
        """Compara todos los titulares y conserva la mejor puntuación por persona."""
        consulta = normalizar(titular)
        if not consulta:
            return ()
        exacta = self._indice_exacto.get(clave_orden(titular))
        if exacta is not None:
            return (_crear_candidato(exacta, 100.0, ViaCoincidencia.EXACTO),)

        candidatos: list[CandidatoCoincidencia] = []
        for persona, nombres in self._nombres:
            mejor: CandidatoCoincidencia | None = None
            for nombre in nombres:
                if not nombre:
                    continue
                candidato = _crear_candidato(
                    persona, self._comparador.similitud(consulta, nombre), ViaCoincidencia.FUZZY
                )
                if mejor is None or candidato.puntuacion > mejor.puntuacion:
                    mejor = candidato
            if mejor is not None:
                candidatos.append(mejor)
        return tuple(
            sorted(candidatos, key=lambda candidato: (-candidato.puntuacion, candidato.persona_id))
        )

    def _pendiente(
        self,
        contacto: ContactoSemanal,
        motivo: MotivoPendiente,
        candidatos: tuple[CandidatoCoincidencia, ...],
    ) -> ContactoPendiente:
        """Limita las sugerencias una vez tomada la decisión de revisión."""
        return ContactoPendiente(
            contacto=contacto,
            motivo=motivo,
            candidatos=candidatos[: self._politica.limite_sugerencias],
        )

    def _resolver_titular(
        self, contacto: ContactoSemanal, titular: str, hojas: frozenset[str]
    ) -> ContactoResuelto | ContactoPendiente:
        """Aplica umbrales y veto por broker sin usar el broker como desempate."""
        candidatos = self._candidatos_para(titular)
        if not candidatos or candidatos[0].puntuacion < self._politica.umbral_revision:
            return self._pendiente(contacto, MotivoPendiente.SIN_MATCH, candidatos)

        mejor = candidatos[0]
        empatados = tuple(
            candidato for candidato in candidatos if candidato.puntuacion == mejor.puntuacion
        )
        if hojas and not any(candidato.hojas & hojas for candidato in empatados):
            return self._pendiente(contacto, MotivoPendiente.BROKER_NO_COINCIDE, candidatos)

        automaticos = tuple(
            candidato
            for candidato in candidatos
            if candidato.puntuacion >= self._politica.umbral_automatico
        )
        if len(empatados) > 1 or len(automaticos) != 1:
            return self._pendiente(contacto, MotivoPendiente.AMBIGUO, candidatos)

        persona = self._personas[mejor.persona_id]
        return ContactoResuelto(
            contacto=contacto,
            persona_id=persona.persona_id,
            ubicaciones=tuple(
                ubicacion
                for ubicacion in persona.ubicaciones
                if not hojas or ubicacion.hoja in hojas
            ),
            puntuacion=mejor.puntuacion,
            via=mejor.via,
        )

    def _resolver_aliases(
        self,
        contacto: ContactoSemanal,
        aliases: tuple[AliasContacto, ...],
        hojas: frozenset[str],
        via: ViaCoincidencia,
    ) -> tuple[ResultadoContacto, ...]:
        """Valida aliases compartidos y destinos contra el catálogo vigente."""
        identificadores = sorted(
            {destino.persona_id for alias in aliases for destino in alias.personas}
        )
        candidatos = tuple(
            _crear_candidato(self._personas[identificador], 100.0, via)
            for identificador in identificadores
            if identificador in self._personas
        )
        destinos = frozenset(aliases[0].personas)
        telefono_alias = aliases[0].telefono
        if (
            via is ViaCoincidencia.ALIAS
            and telefono_alias is not None
            and contacto.telefono is not None
            and telefono_alias != contacto.telefono
        ):
            return (self._pendiente(contacto, MotivoPendiente.AMBIGUO, candidatos),)
        if any(frozenset(alias.personas) != destinos for alias in aliases):
            return (self._pendiente(contacto, MotivoPendiente.AMBIGUO, candidatos),)
        if any(destino.persona_id not in self._personas for destino in destinos):
            return (self._pendiente(contacto, MotivoPendiente.SIN_MATCH, candidatos),)

        resultados: list[ResultadoContacto] = []
        for destino in sorted(destinos, key=lambda destino: destino.persona_id):
            persona = self._personas[destino.persona_id]
            if not destino.hojas <= persona.hojas:
                return (self._pendiente(contacto, MotivoPendiente.BROKER_NO_COINCIDE, candidatos),)
            ubicaciones = tuple(
                ubicacion
                for ubicacion in persona.ubicaciones
                if ubicacion.hoja in destino.hojas and (not hojas or ubicacion.hoja in hojas)
            )
            if not ubicaciones:
                return (self._pendiente(contacto, MotivoPendiente.BROKER_NO_COINCIDE, candidatos),)
            resultados.append(
                ContactoResuelto(
                    contacto=contacto,
                    persona_id=persona.persona_id,
                    ubicaciones=ubicaciones,
                    puntuacion=100.0,
                    via=via,
                )
            )
        return tuple(resultados)
