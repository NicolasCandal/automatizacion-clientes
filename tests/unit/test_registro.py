"""Planes completos, simulación, precondiciones y recuperación tras fallos de registro."""

from collections.abc import Mapping
from dataclasses import FrozenInstanceError, dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from automatizacion_clientes.core.catalogo import construir_catalogo
from automatizacion_clientes.core.errores import (
    AutomatizacionError,
    ConcurrenciaError,
    ConfiguracionError,
    PersistenciaError,
    RegistroInterrumpidoError,
    TiempoEsperaError,
    ValidacionError,
)
from automatizacion_clientes.core.modelos import (
    AparicionCliente,
    CatalogoClientes,
    ContactoIgnorado,
    ContactoPendiente,
    ContactoResuelto,
    ContactoSemanal,
    FilaCliente,
    HojaInfo,
    MotivoIgnorado,
    MotivoPendiente,
    PeriodoSemanal,
    ResolucionSemanal,
    SemanaContactos,
    UbicacionCliente,
    ViaCoincidencia,
)
from automatizacion_clientes.core.registro import (
    Encabezados,
    MarcasHoja,
    PlanRegistro,
    ResultadoRegistro,
    clientes_del_catalogo,
    construir_operaciones,
)
from automatizacion_clientes.services.registro import ServicioRegistro


class PlanillaMemoria:
    def __init__(self) -> None:
        self.numero_revision = 0
        self.hojas: tuple[HojaInfo, ...] = (
            HojaInfo(
                nombre="IOL",
                sheet_id=0,
                col_nombre=1,
                primera_col_semana=3,
                ultima_col_semana=5,
                ultima_fila_con_cliente=8,
            ),
            HojaInfo(
                nombre="Balanz",
                sheet_id=1,
                col_nombre=1,
                primera_col_semana=None,
                ultima_col_semana=None,
                ultima_fila_con_cliente=3,
            ),
            HojaInfo(
                nombre="PPI",
                sheet_id=2,
                col_nombre=1,
                primera_col_semana=None,
                ultima_col_semana=None,
                ultima_fila_con_cliente=2,
            ),
        )
        self.clientes = {
            "IOL": (
                FilaCliente(fila=3, nombre_crudo="Axa Prueba"),
                FilaCliente(fila=5, nombre_crudo="Notuka Ejemplo"),
                FilaCliente(fila=8, nombre_crudo="Prueba, Axa"),
            ),
            "Balanz": (FilaCliente(fila=3, nombre_crudo="Axa Prueba"),),
            "PPI": (),
        }
        self.encabezados: dict[str, Encabezados] = {
            "IOL": ((3, " Semana actual "), (5, "Posterior")),
            "Balanz": (),
            "PPI": (),
        }
        self.marcas = {("IOL", 3): frozenset({5}), ("IOL", 5): frozenset({3})}
        self.respaldos = 0
        self.escrituras: list[tuple[str, int, str, frozenset[int], str]] = []

    def revision(self) -> str:
        return f"revision_{self.numero_revision}"

    def listar_hojas(self) -> tuple[HojaInfo, ...]:
        return self.hojas

    def leer_clientes(self, hoja: str) -> tuple[FilaCliente, ...]:
        return self.clientes[hoja]

    def leer_encabezados(self, hoja: str) -> Encabezados:
        return self.encabezados[hoja]

    def leer_marcas(self, hoja: str, columna: int) -> frozenset[int]:
        return self.marcas.get((hoja, columna), frozenset())

    def ultima_semana(self, hoja: str) -> tuple[int, str]:
        return self.encabezados[hoja][-1] if self.encabezados[hoja] else (1, "")

    def crear_respaldo(self) -> Path:
        self.respaldos += 1
        return Path("respaldo_sintetico.xlsx")

    def aplicar_marcas(
        self,
        hoja: str,
        columna: int,
        encabezado: str,
        filas: frozenset[int],
        *,
        revision_esperada: str,
    ) -> None:
        assert self.respaldos > 0
        if revision_esperada != self.revision():
            raise ConcurrenciaError("Cambio simulado.")
        self.escrituras.append((hoja, columna, encabezado, filas, revision_esperada))
        if columna not in {indice for indice, _ in self.encabezados[hoja]}:
            self.encabezados[hoja] = (*self.encabezados[hoja], (columna, encabezado))
            self.hojas = tuple(
                replace(
                    info,
                    primera_col_semana=info.primera_col_semana or columna,
                    ultima_col_semana=columna,
                )
                if info.nombre == hoja
                else info
                for info in self.hojas
            )
        self.marcas[(hoja, columna)] = filas
        self.numero_revision += 1


@dataclass
class Entorno:
    fuente: PlanillaMemoria
    catalogo: CatalogoClientes
    semana: SemanaContactos
    resolucion: ResolucionSemanal

    def servicio(self, primeras: Mapping[str, int] | None = None) -> ServicioRegistro:
        return ServicioRegistro(
            lector_clientes=self.fuente,
            escritor_clientes=self.fuente,
            identificador_origen="origen_sintetico",
            primeras_columnas=primeras,
        )

    def plan(self, servicio: ServicioRegistro | None = None) -> PlanRegistro:
        return (servicio or self.servicio()).planificar(
            catalogo=self.catalogo,
            semana=self.semana,
            resolucion=self.resolucion,
            encabezado="Semana actual",
        )


@pytest.fixture
def entorno() -> Entorno:
    fuente = PlanillaMemoria()
    catalogo = construir_catalogo(
        identificador_origen="origen_sintetico",
        revision=fuente.revision(),
        generado_en=datetime(2026, 11, 5, tzinfo=UTC),
        hojas=fuente.hojas,
        clientes_por_hoja=fuente.clientes,
    )
    contacto = ContactoSemanal(nombre_crudo="Axa Prueba", ultima_fecha=date(2026, 11, 5))
    persona = next(persona for persona in catalogo.personas if persona.clave_orden == "axa prueba")
    return Entorno(
        fuente=fuente,
        catalogo=catalogo,
        semana=SemanaContactos(
            periodo=PeriodoSemanal(desde=date(2026, 11, 2), hasta=date(2026, 11, 6)),
            contactos=(contacto,),
        ),
        resolucion=ResolucionSemanal(
            resueltos=(
                ContactoResuelto(
                    contacto=contacto,
                    persona_id=persona.persona_id,
                    ubicaciones=persona.ubicaciones,
                    puntuacion=100.0,
                    via=ViaCoincidencia.EXACTO,
                ),
            )
        ),
    )


def test_constructor_no_consulta_los_puertos(entorno: Entorno, mocker: MockerFixture) -> None:
    lectura = mocker.spy(entorno.fuente, "revision")
    respaldo = mocker.spy(entorno.fuente, "crear_respaldo")
    entorno.servicio()
    lectura.assert_not_called()
    respaldo.assert_not_called()


def test_rechaza_identidad_vacia(entorno: Entorno) -> None:
    with pytest.raises(ConfiguracionError):
        ServicioRegistro(
            lector_clientes=entorno.fuente,
            escritor_clientes=entorno.fuente,
            identificador_origen=" ",
        )


def test_plan_recupera_semana_historica_y_todas_las_filas_sin_escribir(entorno: Entorno) -> None:
    plan = entorno.plan()
    assert tuple(
        (operacion.hoja, operacion.columna, operacion.filas, operacion.nueva_semana)
        for operacion in plan.operaciones
    ) == (
        ("IOL", 3, frozenset({3, 8}), False),
        ("Balanz", 4, frozenset({3}), True),
        ("PPI", 3, frozenset(), True),
    )
    assert plan.marcas_anteriores == (frozenset({5}), frozenset(), frozenset())
    assert entorno.fuente.respaldos == 0
    assert entorno.fuente.escrituras == []


def test_simulacion_es_predeterminada_y_revalida_sin_efectos(entorno: Entorno) -> None:
    servicio = entorno.servicio()
    plan = entorno.plan(servicio)
    resultado = servicio.ejecutar(plan)
    assert resultado.simulacion
    assert resultado.plan == plan
    assert resultado.respaldo is None
    assert resultado.hojas_aplicadas == ()
    assert resultado.revision_final == "revision_0"
    assert entorno.fuente.respaldos == 0
    assert entorno.fuente.escrituras == []


def test_aplica_tras_un_respaldo_y_actualiza_revision_entre_hojas(entorno: Entorno) -> None:
    resultado = entorno.servicio().ejecutar(entorno.plan(), simulacion=False)
    assert resultado.hojas_aplicadas == ("IOL", "Balanz", "PPI")
    assert resultado.respaldo == Path("respaldo_sintetico.xlsx")
    assert entorno.fuente.respaldos == 1
    assert tuple(escritura[-1] for escritura in entorno.fuente.escrituras) == (
        "revision_0",
        "revision_1",
        "revision_2",
    )
    assert resultado.revision_final == "revision_3"
    assert entorno.fuente.marcas[("IOL", 3)] == frozenset({3, 8})
    assert entorno.fuente.marcas[("IOL", 5)] == frozenset({3})


def test_semana_nueva_se_anade_despues_de_la_ultima_sin_rellenar_huecos(entorno: Entorno) -> None:
    plan = entorno.servicio().planificar(
        catalogo=entorno.catalogo,
        semana=entorno.semana,
        resolucion=entorno.resolucion,
        encabezado="Otra semana",
    )
    assert plan.operaciones[0].columna == 6


def test_repeticion_con_catalogo_refrescado_no_respalda_ni_reescribe(entorno: Entorno) -> None:
    servicio = entorno.servicio()
    servicio.ejecutar(entorno.plan(), simulacion=False)
    entorno.catalogo = construir_catalogo(
        identificador_origen="origen_sintetico",
        revision=entorno.fuente.revision(),
        generado_en=entorno.catalogo.generado_en,
        hojas=entorno.fuente.hojas,
        clientes_por_hoja=entorno.fuente.clientes,
        anterior=entorno.catalogo,
    )
    resultado = servicio.ejecutar(entorno.plan(), simulacion=False)
    assert not resultado.simulacion
    assert resultado.respaldo is None
    assert resultado.hojas_aplicadas == ()
    assert entorno.fuente.respaldos == 1
    assert len(entorno.fuente.escrituras) == 3


def test_catalogo_de_otro_origen_no_respalda(entorno: Entorno) -> None:
    entorno.catalogo = replace(entorno.catalogo, identificador_origen="otro")
    with pytest.raises(ConfiguracionError):
        entorno.plan()
    assert entorno.fuente.respaldos == 0


@pytest.mark.parametrize("simulacion", [True, False])
def test_cambio_entre_plan_y_ejecucion_invalida_el_plan(entorno: Entorno, simulacion: bool) -> None:
    plan = entorno.plan()
    entorno.fuente.numero_revision += 1
    with pytest.raises(ConcurrenciaError):
        entorno.servicio().ejecutar(plan, simulacion=simulacion)
    assert entorno.fuente.respaldos == 0
    assert entorno.fuente.escrituras == []


@pytest.mark.parametrize("cambio", ["clientes", "encabezados", "estructura"])
def test_compara_contenido_y_no_solo_el_identificador_de_revision(
    entorno: Entorno, cambio: str
) -> None:
    plan = entorno.plan()
    if cambio == "clientes":
        entorno.fuente.clientes["IOL"] = (FilaCliente(fila=3, nombre_crudo="Otra Persona"),)
    elif cambio == "encabezados":
        entorno.fuente.encabezados["IOL"] = ((3, "Semana actual"), (5, "Cambiado"))
    else:
        entorno.fuente.hojas = (
            *entorno.fuente.hojas[:-1],
            replace(entorno.fuente.hojas[-1], sheet_id=9),
        )
    with pytest.raises(ConcurrenciaError):
        entorno.servicio().ejecutar(plan, simulacion=False)
    assert entorno.fuente.respaldos == 0


def test_cambio_de_marcas_sin_revision_invalida_precondiciones(entorno: Entorno) -> None:
    plan = entorno.plan()
    entorno.fuente.marcas[("IOL", 3)] = frozenset({3})
    with pytest.raises(ConcurrenciaError):
        entorno.servicio().ejecutar(plan, simulacion=False)
    assert entorno.fuente.respaldos == 0


def test_plan_alterado_no_se_aplica(entorno: Entorno) -> None:
    plan = entorno.plan()
    alterado = replace(
        plan,
        operaciones=(replace(plan.operaciones[0], filas=frozenset({5})), *plan.operaciones[1:]),
    )
    with pytest.raises(ConcurrenciaError):
        entorno.servicio().ejecutar(alterado, simulacion=False)
    assert entorno.fuente.respaldos == 0


def test_fallo_respaldo_se_propaga_sin_escribir(entorno: Entorno, mocker: MockerFixture) -> None:
    causa = PersistenciaError("Fallo al respaldar.")
    mocker.patch.object(entorno.fuente, "crear_respaldo", side_effect=causa)
    with pytest.raises(PersistenciaError) as error:
        entorno.servicio().ejecutar(entorno.plan(), simulacion=False)
    assert error.value is causa
    assert entorno.fuente.escrituras == []


def test_cambio_durante_respaldo_conserva_ruta_sin_escribir(
    entorno: Entorno, mocker: MockerFixture
) -> None:
    def respaldar_con_cambio() -> Path:
        entorno.fuente.numero_revision += 1
        return Path("respaldo_previo.xlsx")

    mocker.patch.object(entorno.fuente, "crear_respaldo", side_effect=respaldar_con_cambio)
    with pytest.raises(RegistroInterrumpidoError) as error:
        entorno.servicio().ejecutar(entorno.plan(), simulacion=False)
    assert isinstance(error.value.__cause__, ConcurrenciaError)
    assert error.value.respaldo == Path("respaldo_previo.xlsx")
    assert error.value.hojas_confirmadas == ()
    assert error.value.hoja_en_curso is None
    assert entorno.fuente.escrituras == []


@pytest.mark.parametrize(
    "causa",
    [
        PersistenciaError("Fallo."),
        TiempoEsperaError("Incierto."),
        RuntimeError("Fallo no traducido."),
    ],
)
def test_fallo_intermedio_informa_estado_y_no_reintenta(
    entorno: Entorno, mocker: MockerFixture, causa: Exception
) -> None:
    escribir = entorno.fuente.aplicar_marcas

    def aplicar(
        hoja: str, columna: int, encabezado: str, filas: frozenset[int], *, revision_esperada: str
    ) -> None:
        if hoja == "Balanz":
            raise causa
        escribir(hoja, columna, encabezado, filas, revision_esperada=revision_esperada)

    escritura = mocker.patch.object(entorno.fuente, "aplicar_marcas", side_effect=aplicar)
    with pytest.raises(RegistroInterrumpidoError) as error:
        entorno.servicio().ejecutar(entorno.plan(), simulacion=False)
    assert error.value.__cause__ is causa
    assert isinstance(error.value, AutomatizacionError)
    assert error.value.CODIGO == "REGISTRO_INTERRUMPIDO"
    assert error.value.hojas_confirmadas == ("IOL",)
    assert error.value.hoja_en_curso == "Balanz"
    assert escritura.call_count == 2
    assert entorno.fuente.encabezados["PPI"] == ()


def test_no_confirma_una_escritura_que_el_origen_no_refleja(
    entorno: Entorno, mocker: MockerFixture
) -> None:
    escritura = mocker.patch.object(entorno.fuente, "aplicar_marcas", return_value=None)
    with pytest.raises(RegistroInterrumpidoError) as error:
        entorno.servicio().ejecutar(entorno.plan(), simulacion=False)
    assert isinstance(error.value.__cause__, ConcurrenciaError)
    assert error.value.hojas_confirmadas == ()
    assert error.value.hoja_en_curso == "IOL"
    assert escritura.call_count == 1


def test_cambio_de_clientes_tras_primera_escritura_detiene_las_demas(
    entorno: Entorno, mocker: MockerFixture
) -> None:
    escribir = entorno.fuente.aplicar_marcas

    def escribir_y_mover(
        hoja: str, columna: int, encabezado: str, filas: frozenset[int], *, revision_esperada: str
    ) -> None:
        escribir(hoja, columna, encabezado, filas, revision_esperada=revision_esperada)
        entorno.fuente.clientes["Balanz"] = (FilaCliente(fila=3, nombre_crudo="Cliente externo"),)

    escritura = mocker.patch.object(entorno.fuente, "aplicar_marcas", side_effect=escribir_y_mover)
    with pytest.raises(RegistroInterrumpidoError):
        entorno.servicio().ejecutar(entorno.plan(), simulacion=False)
    assert escritura.call_count == 1


def test_revision_cambiante_durante_lectura_invalida_plan(
    entorno: Entorno, mocker: MockerFixture
) -> None:
    leer = entorno.fuente.leer_clientes

    def leer_y_cambiar(hoja: str) -> tuple[FilaCliente, ...]:
        entorno.fuente.numero_revision += 1
        return leer(hoja)

    mocker.patch.object(entorno.fuente, "leer_clientes", side_effect=leer_y_cambiar)
    with pytest.raises(ConcurrenciaError):
        entorno.plan()


def test_cambio_antes_de_verificacion_invalida_revision_original(
    entorno: Entorno, mocker: MockerFixture
) -> None:
    consulta = entorno.fuente.leer_marcas

    def consultar(hoja: str, columna: int) -> frozenset[int]:
        entorno.fuente.numero_revision = 1
        return consulta(hoja, columna)

    mocker.patch.object(entorno.fuente, "leer_marcas", side_effect=consultar)
    with pytest.raises(ConcurrenciaError):
        entorno.plan()


def test_tabla_personalizada_se_copia_y_no_importa_constantes_de_excel(entorno: Entorno) -> None:
    primeras = {"IOL": 3, "Balanz": 6, "PPI": 7}
    servicio = entorno.servicio(primeras)
    primeras["Balanz"] = 9
    assert entorno.plan(servicio).operaciones[1].columna == 6


@pytest.mark.parametrize("primeras", [{}, {"IOL": 1}, {"IOL": True}])
def test_configuracion_de_columnas_incompleta_o_invalida_se_rechaza(
    entorno: Entorno, primeras: dict[str, int]
) -> None:
    with pytest.raises(ValidacionError):
        entorno.plan(entorno.servicio(primeras))


@pytest.mark.parametrize("encabezado", ["", " Semana actual", "Semana actual "])
def test_encabezado_debe_ser_canonico(entorno: Entorno, encabezado: str) -> None:
    with pytest.raises(ValidacionError):
        entorno.servicio().planificar(
            catalogo=entorno.catalogo,
            semana=entorno.semana,
            resolucion=entorno.resolucion,
            encabezado=encabezado,
        )


def test_pendientes_bloquean_por_defecto_y_su_uso_parcial_es_explicito(entorno: Entorno) -> None:
    contacto = ContactoSemanal(nombre_crudo="Sin coincidencia", ultima_fecha=date(2026, 11, 5))
    entorno.semana = replace(entorno.semana, contactos=(*entorno.semana.contactos, contacto))
    entorno.resolucion = replace(
        entorno.resolucion,
        pendientes=(ContactoPendiente(contacto=contacto, motivo=MotivoPendiente.SIN_MATCH),),
    )
    with pytest.raises(ValidacionError, match="pendientes"):
        entorno.plan()
    plan = entorno.servicio().planificar(
        catalogo=entorno.catalogo,
        semana=entorno.semana,
        resolucion=entorno.resolucion,
        encabezado="Semana actual",
        permitir_pendientes=True,
    )
    assert plan.operaciones[0].filas == frozenset({3, 8})
    assert plan.permitir_pendientes


@pytest.mark.parametrize("caso", ["ausente", "extrano", "duplicado_omitido"])
def test_resolucion_debe_cubrir_la_semana_recibida(entorno: Entorno, caso: str) -> None:
    if caso == "ausente":
        entorno.resolucion = ResolucionSemanal()
    elif caso == "extrano":
        entorno.semana = replace(entorno.semana, contactos=())
    else:
        entorno.semana = replace(entorno.semana, contactos=entorno.semana.contactos * 2)
    with pytest.raises(ValidacionError, match="cubrir"):
        entorno.plan()


def test_contactos_repetidos_y_cotitulares_no_duplican_las_filas(entorno: Entorno) -> None:
    entorno.semana = replace(entorno.semana, contactos=entorno.semana.contactos * 2)
    entorno.resolucion = replace(entorno.resolucion, resueltos=entorno.resolucion.resueltos * 2)
    assert entorno.plan().operaciones[0].filas == frozenset({3, 8})


@pytest.mark.parametrize("caso", ["persona_ausente", "fila_ajena", "fila_omitida", "grupo"])
def test_rechaza_destinos_ajenos_parciales_y_grupos(entorno: Entorno, caso: str) -> None:
    resuelto = entorno.resolucion.resueltos[0]
    if caso == "persona_ausente":
        resuelto = replace(resuelto, persona_id="inexistente")
    elif caso == "fila_ajena":
        resuelto = replace(resuelto, ubicaciones=(UbicacionCliente(hoja="IOL", fila=5),))
    elif caso == "fila_omitida":
        resuelto = replace(resuelto, ubicaciones=(UbicacionCliente(hoja="IOL", fila=3),))
    else:
        contacto = replace(resuelto.contacto, es_grupo=True)
        resuelto = replace(resuelto, contacto=contacto)
        entorno.semana = replace(entorno.semana, contactos=(contacto,))
    entorno.resolucion = replace(entorno.resolucion, resueltos=(resuelto,))
    with pytest.raises(ValidacionError):
        entorno.plan()


def test_respetar_brokers_permite_subconjunto_de_hojas_con_todas_sus_filas(
    entorno: Entorno,
) -> None:
    resuelto = entorno.resolucion.resueltos[0]
    entorno.resolucion = replace(
        entorno.resolucion,
        resueltos=(
            replace(
                resuelto,
                ubicaciones=tuple(
                    ubicacion for ubicacion in resuelto.ubicaciones if ubicacion.hoja == "IOL"
                ),
            ),
        ),
    )
    plan = entorno.plan()
    assert plan.operaciones[0].filas == frozenset({3, 8})
    assert plan.operaciones[1].filas == frozenset()


def test_semana_vacia_es_un_reemplazo_explicito_sin_marcas(entorno: Entorno) -> None:
    entorno.semana = replace(entorno.semana, contactos=())
    entorno.resolucion = ResolucionSemanal()
    assert all(not operacion.filas for operacion in entorno.plan().operaciones)


def test_grupos_ignorados_cubren_contactos_sin_marcar(entorno: Entorno) -> None:
    grupo = replace(entorno.semana.contactos[0], es_grupo=True)
    entorno.semana = replace(entorno.semana, contactos=(grupo,))
    entorno.resolucion = ResolucionSemanal(
        ignorados=(ContactoIgnorado(contacto=grupo, motivo=MotivoIgnorado.GRUPO),)
    )
    assert all(not operacion.filas for operacion in entorno.plan().operaciones)


@pytest.mark.parametrize(
    "columnas",
    [
        ((3, "Semana actual"), (3, "Duplicado")),
        ((5, "Posterior"), (3, "Semana actual")),
        ((2, "Fija"), (5, "Posterior")),
        ((3, ""), (5, "Posterior")),
        ((3, "Semana actual"),),
        ((3, "Semana actual"), (5, " Semana actual ")),
    ],
)
def test_encabezados_ambiguos_o_fuera_de_estructura_no_producen_plan(
    entorno: Entorno, columnas: Encabezados
) -> None:
    entorno.fuente.encabezados["IOL"] = columnas
    with pytest.raises(ValidacionError):
        entorno.plan()


def test_falta_de_hojas_o_encabezados_no_produce_operaciones(entorno: Entorno) -> None:
    with pytest.raises(ValidacionError):
        construir_operaciones(
            catalogo=entorno.catalogo,
            semana=entorno.semana,
            resolucion=entorno.resolucion,
            encabezado="Semana actual",
            encabezados={},
            primeras_columnas={},
        )
    entorno.catalogo = replace(entorno.catalogo, hojas=(), personas=())
    entorno.semana = replace(entorno.semana, contactos=())
    entorno.resolucion = ResolucionSemanal()
    with pytest.raises(ValidacionError):
        entorno.plan()


def test_catalogo_rechaza_textos_contradictorios_en_una_fila_compartida(entorno: Entorno) -> None:
    primera, segunda = entorno.catalogo.personas
    ubicacion = primera.apariciones[0].ubicacion
    segunda = replace(
        segunda,
        apariciones=(AparicionCliente(ubicacion=ubicacion, nombre_crudo="Texto contradictorio"),),
    )
    with pytest.raises(ValidacionError):
        clientes_del_catalogo(replace(entorno.catalogo, personas=(primera, segunda)))


def test_modelos_inmutables_y_representacion_sin_datos_personales(entorno: Entorno) -> None:
    plan = entorno.plan()
    with pytest.raises(FrozenInstanceError):
        plan.__setattr__("encabezado", "Cambio")
    assert "Axa Prueba" not in repr(plan)
    assert "Notuka Ejemplo" not in repr(plan)


@pytest.mark.parametrize(
    ("columna", "filas"),
    [(0, frozenset({3})), (True, frozenset({3})), (3, frozenset({True})), (3, frozenset({0}))],
)
def test_operaciones_rechazan_indices_invalidos(columna: int, filas: frozenset[int]) -> None:
    with pytest.raises(ValidacionError):
        MarcasHoja(
            hoja="IOL", columna=columna, encabezado="Semana", filas=filas, nueva_semana=False
        )


def test_operacion_requiere_textos_no_vacios() -> None:
    with pytest.raises(ValidacionError):
        MarcasHoja(hoja=" ", columna=3, encabezado="Semana", filas=frozenset(), nueva_semana=False)


@pytest.mark.parametrize("campo", ["operaciones", "encabezados_anteriores", "marcas_anteriores"])
def test_plan_requiere_precondiciones_alineadas(entorno: Entorno, campo: str) -> None:
    plan = entorno.plan()
    with pytest.raises(ValidacionError):
        if campo == "operaciones":
            replace(plan, operaciones=())
        elif campo == "encabezados_anteriores":
            replace(plan, encabezados_anteriores=())
        else:
            replace(plan, marcas_anteriores=())


@pytest.mark.parametrize(
    "caso", ["simulacion", "sin_respaldo", "revision", "hoja_ajena", "duplicadas"]
)
def test_resultado_rechaza_efectos_incoherentes(entorno: Entorno, caso: str) -> None:
    resultado = ResultadoRegistro(
        plan=entorno.plan(),
        simulacion=False,
        respaldo=Path("copia.xlsx"),
        hojas_aplicadas=("IOL",),
        revision_final="revision_final",
    )
    with pytest.raises(ValidacionError):
        if caso == "simulacion":
            replace(resultado, simulacion=True)
        elif caso == "sin_respaldo":
            replace(resultado, respaldo=None)
        elif caso == "revision":
            replace(resultado, revision_final=" ")
        elif caso == "hoja_ajena":
            replace(resultado, hojas_aplicadas=("Ajena",))
        else:
            replace(resultado, hojas_aplicadas=("IOL", "IOL"))


def test_logs_no_exponen_causa_rutas_o_contactos(
    entorno: Entorno, mocker: MockerFixture, caplog: pytest.LogCaptureFixture
) -> None:
    mocker.patch.object(
        entorno.fuente, "aplicar_marcas", side_effect=PersistenciaError("Axa Prueba; ruta secreta")
    )
    with caplog.at_level("INFO"), pytest.raises(RegistroInterrumpidoError) as error:
        entorno.servicio().ejecutar(entorno.plan(), simulacion=False)
    assert "Registro interrumpido" in caplog.text
    assert "Axa Prueba" not in caplog.text
    assert "ruta secreta" not in caplog.text
    assert "respaldo_sintetico" not in caplog.text
    assert "ruta secreta" not in str(error.value)
