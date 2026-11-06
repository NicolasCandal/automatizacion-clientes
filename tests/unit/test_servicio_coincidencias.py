"""Orquestación con puertos simulados, sin red, archivos ni credenciales."""

import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from automatizacion_clientes.core.aliases import conciliar_alias, proponer_alias
from automatizacion_clientes.core.contratos import (
    AlmacenAliases,
    AlmacenCatalogo,
    AlmacenResoluciones,
    ComparadorNombres,
    LectorContactos,
    ListaExclusion,
)
from automatizacion_clientes.core.errores import (
    ConcurrenciaError,
    ConfiguracionError,
    PersistenciaError,
    ValidacionError,
)
from automatizacion_clientes.core.modelos import (
    AliasContacto,
    AparicionCliente,
    CatalogoClientes,
    ContactoSemanal,
    DestinoAlias,
    HojaInfo,
    MotivoIgnorado,
    MotivoPendiente,
    PeriodoSemanal,
    PersonaCliente,
    PoliticaCoincidencia,
    ResolucionSemanal,
    SemanaContactos,
    UbicacionCliente,
    ViaCoincidencia,
)
from automatizacion_clientes.core.normalizacion import clave_orden
from automatizacion_clientes.services.coincidencias import ServicioCoincidencias


@dataclass
class Entorno:
    lector: MagicMock
    catalogos: MagicMock
    aliases: MagicMock
    exclusiones: MagicMock
    informes: MagicMock
    comparador: MagicMock

    def servicio(
        self,
        politica: PoliticaCoincidencia | None = None,
        tabla_brokers: Mapping[str, str] | None = None,
    ) -> ServicioCoincidencias:
        return ServicioCoincidencias(
            lector_contactos=self.lector,
            almacen_catalogo=self.catalogos,
            almacen_aliases=self.aliases,
            lista_exclusion=self.exclusiones,
            almacen_resoluciones=self.informes,
            comparador=self.comparador,
            politica=politica,
            tabla_brokers=tabla_brokers,
        )


@pytest.fixture
def contacto() -> ContactoSemanal:
    return ContactoSemanal(
        nombre_crudo="Axa Prueba (IOL)", ultima_fecha=date(2026, 11, 5), telefono="+5491100000000"
    )


@pytest.fixture
def semana(contacto: ContactoSemanal) -> SemanaContactos:
    return SemanaContactos(
        periodo=PeriodoSemanal(desde=date(2026, 11, 2), hasta=date(2026, 11, 6)),
        contactos=(contacto,),
    )


@pytest.fixture
def catalogo() -> CatalogoClientes:
    return CatalogoClientes(
        identificador_origen="origen_sintetico",
        revision="revision_1",
        generado_en=datetime(2026, 11, 5, tzinfo=UTC),
        hojas=(
            HojaInfo(
                nombre="IOL",
                sheet_id=0,
                col_nombre=1,
                primera_col_semana=None,
                ultima_col_semana=None,
                ultima_fila_con_cliente=100,
            ),
        ),
        personas=tuple(
            PersonaCliente(
                persona_id=identificador,
                clave_orden=clave_orden(nombre),
                nombre_canonico=nombre,
                titulares=(nombre,),
                apariciones=(
                    AparicionCliente(
                        ubicacion=UbicacionCliente(hoja="IOL", fila=fila), nombre_crudo=nombre
                    ),
                ),
            )
            for identificador, nombre, fila in (
                ("p0001", "Axa Prueba", 3),
                ("p0002", "Notuka Ejemplo", 4),
            )
        ),
    )


@pytest.fixture
def entorno(mocker: MockerFixture, semana: SemanaContactos, catalogo: CatalogoClientes) -> Entorno:
    fuentes = Entorno(
        lector=mocker.create_autospec(LectorContactos, instance=True, spec_set=True),
        catalogos=mocker.create_autospec(AlmacenCatalogo, instance=True, spec_set=True),
        aliases=mocker.create_autospec(AlmacenAliases, instance=True, spec_set=True),
        exclusiones=mocker.create_autospec(ListaExclusion, instance=True, spec_set=True),
        informes=mocker.create_autospec(AlmacenResoluciones, instance=True, spec_set=True),
        comparador=mocker.create_autospec(ComparadorNombres, instance=True, spec_set=True),
    )
    fuentes.lector.leer.return_value = semana
    fuentes.catalogos.leer.return_value = catalogo
    fuentes.aliases.buscar_por_nombre.return_value = None
    fuentes.aliases.buscar_por_telefono.return_value = ()
    fuentes.aliases.registrar.return_value = None
    fuentes.exclusiones.contiene.return_value = False
    fuentes.comparador.similitud.return_value = 0.0
    return fuentes


def test_constructor_no_ejecuta_puertos(entorno: Entorno) -> None:
    entorno.servicio()
    assert not entorno.lector.mock_calls
    assert not entorno.catalogos.mock_calls
    assert not entorno.aliases.mock_calls
    assert not entorno.exclusiones.mock_calls
    assert not entorno.informes.mock_calls


def test_instantaneas_del_flujo_evitan_releer_catalogo_y_contactos(
    entorno: Entorno,
    semana: SemanaContactos,
    catalogo: CatalogoClientes,
) -> None:
    entorno.lector.leer.side_effect = RuntimeError("No debe releerse.")
    entorno.catalogos.leer.side_effect = RuntimeError("No debe releerse.")
    resultado = entorno.servicio().ejecutar(catalogo=catalogo, semana=semana)
    assert len(resultado.resueltos) == 1
    entorno.lector.leer.assert_not_called()
    entorno.catalogos.leer.assert_not_called()


def test_simulacion_predeterminada_no_registra_ni_persiste(entorno: Entorno) -> None:
    informe = entorno.servicio().ejecutar()
    assert len(informe.resueltos) == 1
    assert informe.resueltos[0].via is ViaCoincidencia.EXACTO
    entorno.aliases.registrar.assert_not_called()
    entorno.aliases.guardar.assert_not_called()
    entorno.informes.guardar.assert_not_called()
    entorno.exclusiones.agregar.assert_not_called()
    entorno.catalogos.guardar.assert_not_called()


def test_real_guarda_informe_antes_de_aprender_y_guarda_aliases_una_vez(
    entorno: Entorno, contacto: ContactoSemanal, mocker: MockerFixture
) -> None:
    secuencia = mocker.Mock()
    secuencia.attach_mock(entorno.informes.guardar, "informe")
    secuencia.attach_mock(entorno.aliases.registrar, "registrar")
    secuencia.attach_mock(entorno.aliases.guardar, "guardar")

    informe = entorno.servicio().ejecutar(simulacion=False)

    assert [llamada[0] for llamada in secuencia.mock_calls] == ["informe", "registrar", "guardar"]
    entorno.informes.guardar.assert_called_once_with(informe)
    entorno.aliases.registrar.assert_called_once_with(
        AliasContacto(
            nombre_crudo=contacto.nombre_crudo,
            telefono=contacto.telefono,
            personas=(DestinoAlias(persona_id="p0001", hojas=frozenset({"IOL"})),),
        )
    )


def test_ausencia_de_catalogo_no_inicia_aprendizaje(entorno: Entorno) -> None:
    entorno.catalogos.leer.return_value = None
    with pytest.raises(ConfiguracionError, match="catálogo"):
        entorno.servicio().ejecutar(simulacion=False)
    assert not entorno.aliases.mock_calls
    entorno.informes.guardar.assert_not_called()


@pytest.mark.parametrize("grupo", [True, False])
def test_grupos_y_excluidos_no_consultan_aliases(
    entorno: Entorno, semana: SemanaContactos, contacto: ContactoSemanal, grupo: bool
) -> None:
    entorno.lector.leer.return_value = replace(
        semana, contactos=(replace(contacto, es_grupo=grupo),)
    )
    entorno.exclusiones.contiene.return_value = True
    informe = entorno.servicio().ejecutar(simulacion=False)
    assert informe.ignorados[0].motivo is (
        MotivoIgnorado.GRUPO if grupo else MotivoIgnorado.EN_LISTA_NEGRA
    )
    assert not entorno.aliases.mock_calls
    if grupo:
        entorno.exclusiones.contiene.assert_not_called()


def test_telefono_compartido_se_entrega_completo_sin_elegir_primero(
    entorno: Entorno, contacto: ContactoSemanal
) -> None:
    entorno.aliases.buscar_por_telefono.return_value = tuple(
        AliasContacto(
            nombre_crudo=nombre,
            telefono=contacto.telefono,
            personas=(DestinoAlias(persona_id=identificador, hojas=frozenset({"IOL"})),),
        )
        for nombre, identificador in (("Alias A", "p0001"), ("Alias B", "p0002"))
    )
    informe = entorno.servicio().ejecutar(simulacion=False)
    assert informe.pendientes[0].motivo is MotivoPendiente.AMBIGUO
    entorno.aliases.buscar_por_nombre.assert_not_called()
    entorno.aliases.registrar.assert_not_called()
    entorno.aliases.guardar.assert_not_called()


@pytest.mark.parametrize("via", [ViaCoincidencia.ALIAS, ViaCoincidencia.TELEFONO])
def test_decisiones_por_alias_no_se_reaprenden(
    entorno: Entorno, contacto: ContactoSemanal, via: ViaCoincidencia
) -> None:
    alias = AliasContacto(
        nombre_crudo=contacto.nombre_crudo,
        telefono=contacto.telefono,
        personas=(DestinoAlias(persona_id="p0001", hojas=frozenset({"IOL"})),),
    )
    if via is ViaCoincidencia.ALIAS:
        entorno.aliases.buscar_por_nombre.return_value = alias
    else:
        entorno.aliases.buscar_por_telefono.return_value = (alias,)
    informe = entorno.servicio().ejecutar(simulacion=False)
    assert informe.resueltos[0].via is via
    entorno.aliases.registrar.assert_not_called()
    entorno.aliases.guardar.assert_not_called()


def test_no_aprende_de_contactos_parcialmente_resueltos(
    entorno: Entorno, semana: SemanaContactos, contacto: ContactoSemanal
) -> None:
    entorno.lector.leer.return_value = replace(
        semana, contactos=(replace(contacto, nombre_crudo="Axa Prueba Y/O ZZZZZZZ (IOL)"),)
    )
    informe = entorno.servicio().ejecutar(simulacion=False)
    assert len(informe.resueltos) == len(informe.pendientes) == 1
    entorno.aliases.registrar.assert_not_called()
    entorno.aliases.guardar.assert_not_called()


def test_cotitulares_se_aprenden_juntos_y_no_se_cachean_filas(
    entorno: Entorno, semana: SemanaContactos, contacto: ContactoSemanal
) -> None:
    entorno.lector.leer.return_value = replace(
        semana, contactos=(replace(contacto, nombre_crudo="Axa Prueba Y/O Notuka Ejemplo (IOL)"),)
    )
    informe = entorno.servicio().ejecutar(simulacion=False)
    assert len(informe.resueltos) == 2
    entorno.aliases.registrar.assert_called_once()
    alias: AliasContacto = entorno.aliases.registrar.call_args.args[0]
    assert tuple(destino.persona_id for destino in alias.personas) == ("p0001", "p0002")
    assert not hasattr(alias.personas[0], "fila")


def test_aprendizaje_no_cambia_decisiones_de_otro_contacto_del_mismo_telefono(
    entorno: Entorno, semana: SemanaContactos, contacto: ContactoSemanal
) -> None:
    entorno.lector.leer.return_value = replace(
        semana, contactos=(contacto, replace(contacto, nombre_crudo="Notuka Ejemplo (IOL)"))
    )
    informe = entorno.servicio().ejecutar(simulacion=False)
    assert tuple(resultado.persona_id for resultado in informe.resueltos) == ("p0001", "p0002")
    assert all(resultado.via is ViaCoincidencia.EXACTO for resultado in informe.resueltos)
    entorno.aliases.buscar_por_telefono.assert_called_once_with(contacto.telefono)


def test_simulacion_y_real_devuelven_mismos_avisos_de_telefono_en_conflicto(
    entorno: Entorno, semana: SemanaContactos, contacto: ContactoSemanal
) -> None:
    entorno.lector.leer.return_value = replace(
        semana, contactos=(contacto, replace(contacto, telefono="+5491199999999"))
    )
    servicio = entorno.servicio()
    simulado = servicio.ejecutar()
    entorno.aliases.registrar.assert_not_called()
    real = servicio.ejecutar(simulacion=False)
    assert simulado == real
    assert len(real.avisos) == 1
    assert "otro teléfono" in real.avisos[0]
    assert contacto.nombre_crudo not in real.avisos[0]
    entorno.aliases.registrar.assert_called_once()
    alias: AliasContacto = entorno.aliases.registrar.call_args.args[0]
    assert alias.telefono == contacto.telefono


def test_repeticiones_equivalentes_completan_telefono_sin_duplicar_registro(
    entorno: Entorno, semana: SemanaContactos, contacto: ContactoSemanal
) -> None:
    entorno.lector.leer.return_value = replace(
        semana, contactos=(replace(contacto, telefono=None), contacto, contacto)
    )
    informe = entorno.servicio().ejecutar(simulacion=False)
    assert len(informe.resueltos) == 3
    assert not informe.avisos
    entorno.aliases.registrar.assert_called_once()
    alias: AliasContacto = entorno.aliases.registrar.call_args.args[0]
    assert alias.telefono == contacto.telefono
    entorno.aliases.buscar_por_nombre.assert_called_once_with(contacto.nombre_crudo)


def test_politica_configurada_controla_comparacion_aproximada(
    entorno: Entorno, catalogo: CatalogoClientes, semana: SemanaContactos, contacto: ContactoSemanal
) -> None:
    entorno.catalogos.leer.return_value = replace(catalogo, personas=catalogo.personas[:1])
    entorno.lector.leer.return_value = replace(
        semana, contactos=(replace(contacto, nombre_crudo="Consulta distinta"),)
    )
    entorno.comparador.similitud.return_value = 90.0
    assert entorno.servicio().ejecutar().pendientes
    politica = PoliticaCoincidencia(umbral_automatico=89.0, umbral_revision=80.0)
    assert entorno.servicio(politica).ejecutar().resueltos[0].via is ViaCoincidencia.FUZZY


def test_semana_vacia_guarda_informe_sin_tocar_aliases(
    entorno: Entorno, semana: SemanaContactos
) -> None:
    entorno.lector.leer.return_value = replace(semana, contactos=())
    informe = entorno.servicio().ejecutar(simulacion=False)
    assert informe == ResolucionSemanal()
    entorno.informes.guardar.assert_called_once_with(informe)
    assert not entorno.aliases.mock_calls


def test_tabla_de_brokers_se_respeta_y_se_copia_al_construir_servicio(
    entorno: Entorno, semana: SemanaContactos, contacto: ContactoSemanal
) -> None:
    entorno.lector.leer.return_value = replace(
        semana, contactos=(replace(contacto, nombre_crudo="Axa Prueba (selector)"),)
    )
    tabla = {"selector": "Bull"}
    servicio = entorno.servicio(tabla_brokers=tabla)
    tabla["selector"] = "IOL"

    informe = servicio.ejecutar()

    assert informe.pendientes[0].motivo is MotivoPendiente.BROKER_NO_COINCIDE


@pytest.mark.parametrize(
    "etapa", ["contactos", "catalogo", "exclusiones", "telefono", "nombre", "comparador"]
)
def test_fallo_durante_resolucion_no_persiste_ni_inicia_aprendizaje(
    entorno: Entorno, semana: SemanaContactos, contacto: ContactoSemanal, etapa: str
) -> None:
    causa = PersistenciaError("Error de prueba sin datos personales")
    operaciones = {
        "contactos": entorno.lector.leer,
        "catalogo": entorno.catalogos.leer,
        "exclusiones": entorno.exclusiones.contiene,
        "telefono": entorno.aliases.buscar_por_telefono,
        "nombre": entorno.aliases.buscar_por_nombre,
        "comparador": entorno.comparador.similitud,
    }
    if etapa == "comparador":
        entorno.lector.leer.return_value = replace(
            semana, contactos=(contacto, replace(contacto, nombre_crudo="ZZZZZZZ"))
        )
    operaciones[etapa].side_effect = causa
    with pytest.raises(PersistenciaError) as capturada:
        entorno.servicio().ejecutar(simulacion=False)
    assert capturada.value is causa
    entorno.aliases.registrar.assert_not_called()
    entorno.aliases.guardar.assert_not_called()
    entorno.informes.guardar.assert_not_called()


def test_fallo_de_informe_no_modifica_aliases_y_permite_reintento(entorno: Entorno) -> None:
    entorno.informes.guardar.side_effect = PersistenciaError("bloqueado")
    servicio = entorno.servicio()
    with pytest.raises(PersistenciaError):
        servicio.ejecutar(simulacion=False)
    entorno.aliases.registrar.assert_not_called()
    entorno.aliases.guardar.assert_not_called()
    entorno.informes.guardar.side_effect = None
    assert servicio.ejecutar(simulacion=False).resueltos


@pytest.mark.parametrize("etapa", ["registrar", "guardar", "conflicto"])
def test_fallo_de_aliases_deja_informe_y_exige_nueva_sesion(entorno: Entorno, etapa: str) -> None:
    if etapa == "conflicto":
        entorno.aliases.registrar.return_value = "Conflicto detectado"
    else:
        getattr(entorno.aliases, etapa).side_effect = PersistenciaError("bloqueado")
    servicio = entorno.servicio()
    with pytest.raises(ConcurrenciaError if etapa == "conflicto" else PersistenciaError):
        servicio.ejecutar(simulacion=False)
    entorno.informes.guardar.assert_called_once()
    with pytest.raises(ConfiguracionError, match="nueva sesión"):
        servicio.ejecutar()
    entorno.lector.leer.assert_called_once()


def test_reglas_puras_rechazan_nombres_y_resultados_de_otro_contacto(
    entorno: Entorno, contacto: ContactoSemanal
) -> None:
    resultado = entorno.servicio().ejecutar().resueltos[0]
    alias = AliasContacto(
        nombre_crudo=contacto.nombre_crudo,
        personas=(DestinoAlias(persona_id="p0001", hojas=frozenset({"IOL"})),),
    )
    with pytest.raises(ValidacionError):
        conciliar_alias(alias, replace(alias, nombre_crudo="Otro nombre"))
    with pytest.raises(ValidacionError):
        proponer_alias(replace(contacto, nombre_crudo="Otro nombre"), (resultado,))
    assert proponer_alias(contacto, ()) is None


@pytest.mark.parametrize("fallo_aliases", [False, True])
def test_logs_no_incluyen_identidades_ni_detalles_tecnicos_privados(
    entorno: Entorno, caplog: pytest.LogCaptureFixture, fallo_aliases: bool
) -> None:
    if fallo_aliases:
        entorno.aliases.guardar.side_effect = PersistenciaError("Detalle secreto de +5491100000000")
    with caplog.at_level(logging.INFO):
        if fallo_aliases:
            with pytest.raises(PersistenciaError):
                entorno.servicio().ejecutar(simulacion=False)
        else:
            entorno.servicio().ejecutar(simulacion=False)

    assert len(caplog.records) == 1
    assert caplog.records[0].exc_info is None
    for identidad in ("Axa Prueba", "+5491100000000", "p0001", "secreto"):
        assert identidad not in caplog.text
    assert ("Coincidencias finalizadas" in caplog.text) is not fallo_aliases
