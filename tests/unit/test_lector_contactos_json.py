"""Validación de la frontera JSON sin modificar la extracción original."""

import logging
from datetime import date
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from automatizacion_clientes.core.contratos import LectorContactos
from automatizacion_clientes.core.errores import EstructuraError, PersistenciaError, ValidacionError
from automatizacion_clientes.core.modelos import ContactoSemanal, PeriodoSemanal, SemanaContactos
from automatizacion_clientes.infra.almacenamiento_json import ValorJson, guardar_json
from automatizacion_clientes.infra.lector_contactos_json import LectorContactosJson


@pytest.fixture
def documento() -> dict[str, ValorJson]:
    return {
        "rango": {"desde": "2026-11-02", "hasta": "2026-11-06"},
        "extraido_en": "2026-11-06T18:04:00-03:00",
        "total_chats_escaneados": 812,
        "contactos": [
            {
                "nombre_crudo": "  Contacto privado (IOL)  ",
                "ultima_fecha": "2026-11-05",
                "telefono": "+5491100000000",
                "es_grupo": False,
            }
        ],
    }


def test_constructor_no_abre_archivos_ni_crea_carpetas(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    ruta = tmp_path / "nueva" / "semana.json"
    lectura = mocker.patch.object(Path, "read_text", side_effect=PermissionError("bloqueado"))

    lector: LectorContactos = LectorContactosJson(ruta)

    assert lector is not None
    lectura.assert_not_called()
    assert not ruta.parent.exists()


def test_formato_documentado_produce_modelos_tipados_sin_modificar_archivo(
    tmp_path: Path, documento: dict[str, ValorJson]
) -> None:
    ruta = tmp_path / "semana.json"
    guardar_json(ruta, documento)
    original = ruta.read_bytes()
    lector: LectorContactos = LectorContactosJson(ruta)

    semana = lector.leer()

    assert semana == SemanaContactos(
        periodo=PeriodoSemanal(desde=date(2026, 11, 2), hasta=date(2026, 11, 6)),
        contactos=(
            ContactoSemanal(
                nombre_crudo="  Contacto privado (IOL)  ",
                ultima_fecha=date(2026, 11, 5),
                telefono="+5491100000000",
                es_grupo=False,
            ),
        ),
    )
    assert ruta.read_bytes() == original
    assert list(tmp_path.iterdir()) == [ruta]


def test_metadatos_y_campos_opcionales_pueden_omitirse(tmp_path: Path) -> None:
    ruta = tmp_path / "semana.json"
    guardar_json(
        ruta,
        {
            "rango": {"desde": "2026-11-05", "hasta": "2026-11-05"},
            "contactos": [{"nombre_crudo": "Nombre", "ultima_fecha": "2026-11-05"}],
        },
    )

    semana = LectorContactosJson(ruta).leer()

    assert semana.contactos == (
        ContactoSemanal(nombre_crudo="Nombre", ultima_fecha=date(2026, 11, 5)),
    )


def test_conserva_limites_inclusivos_orden_grupos_y_contactos_repetidos(tmp_path: Path) -> None:
    ruta = tmp_path / "semana.json"
    contacto: dict[str, ValorJson] = {
        "nombre_crudo": "Grupo de prueba",
        "ultima_fecha": "2026-11-06",
        "es_grupo": True,
    }
    guardar_json(
        ruta,
        {
            "rango": {"desde": "2026-11-02", "hasta": "2026-11-06"},
            "contactos": [
                contacto,
                {"nombre_crudo": "Primero en fecha", "ultima_fecha": "2026-11-02"},
                contacto,
            ],
            "total_chats_escaneados": 3,
        },
    )

    semana = LectorContactosJson(ruta).leer()

    assert tuple(contacto.nombre_crudo for contacto in semana.contactos) == (
        "Grupo de prueba",
        "Primero en fecha",
        "Grupo de prueba",
    )
    assert semana.contactos[0].es_grupo
    assert semana.contactos[0] == semana.contactos[2]
    assert semana.contactos[1].ultima_fecha == date(2026, 11, 2)
    assert semana.contactos[0].ultima_fecha == date(2026, 11, 6)


def test_contacto_sin_nombre_conserva_telefono_sin_inferir_pais(
    tmp_path: Path, documento: dict[str, ValorJson]
) -> None:
    ruta = tmp_path / "semana.json"
    documento["contactos"] = [
        {"nombre_crudo": "", "ultima_fecha": "2026-11-05", "telefono": "00123456"}
    ]
    guardar_json(ruta, documento)

    semana = LectorContactosJson(ruta).leer()

    assert semana.contactos[0].nombre_crudo == ""
    assert semana.contactos[0].telefono == "00123456"


def test_semana_sin_contactos_es_valida(tmp_path: Path, documento: dict[str, ValorJson]) -> None:
    ruta = tmp_path / "semana.json"
    documento["contactos"] = []
    documento["total_chats_escaneados"] = 0
    guardar_json(ruta, documento)

    assert LectorContactosJson(ruta).leer().contactos == ()


def test_cada_lectura_refleja_archivo_actual_y_no_reutiliza_contactos_anteriores(
    tmp_path: Path, documento: dict[str, ValorJson]
) -> None:
    ruta = tmp_path / "semana.json"
    guardar_json(ruta, documento)
    lector = LectorContactosJson(ruta)
    primera = lector.leer()
    documento["contactos"] = []
    guardar_json(ruta, documento)

    assert lector.leer().contactos == ()
    assert len(primera.contactos) == 1
    ruta.write_bytes(b"{incompleto")
    with pytest.raises(EstructuraError):
        lector.leer()


@pytest.mark.parametrize(
    "contenido",
    [
        None,
        [],
        {},
        {"contactos": []},
        {"rango": {"desde": "2026-11-02", "hasta": "2026-11-06"}},
        {"rango": {}, "contactos": []},
        {"rango": [], "contactos": []},
        {"rango": {"desde": "2026-11-02"}, "contactos": []},
        {"rango": {"desde": "2026-11-02", "hasta": "2026-11-06", "otro": 1}, "contactos": []},
        {"rango": {"desde": "2026-11-02", "hasta": "2026-11-06"}, "contactos": {}},
        {
            "rango": {"desde": "2026-11-02", "hasta": "2026-11-06"},
            "contactos": [],
            "mensajes": [],
        },
    ],
)
def test_rechaza_esquema_semanal_incorrecto_sin_modificar_entrada(
    tmp_path: Path, contenido: ValorJson
) -> None:
    ruta = tmp_path / "semana.json"
    guardar_json(ruta, contenido)
    original = ruta.read_bytes()

    with pytest.raises(EstructuraError):
        LectorContactosJson(ruta).leer()

    assert ruta.read_bytes() == original


@pytest.mark.parametrize(
    "entrada",
    [
        None,
        [],
        {},
        {"nombre_crudo": "Nombre"},
        {"ultima_fecha": "2026-11-05"},
        {"nombre_crudo": None, "ultima_fecha": "2026-11-05"},
        {"nombre_crudo": 123, "ultima_fecha": "2026-11-05"},
        {"nombre_crudo": "Nombre", "ultima_fecha": "2026-11-05", "telefono": 123},
        {"nombre_crudo": "Nombre", "ultima_fecha": "2026-11-05", "es_grupo": "false"},
        {"nombre_crudo": "Nombre", "ultima_fecha": "2026-11-05", "es_grupo": 0},
        {"nombre_crudo": "Nombre", "ultima_fecha": "2026-11-05", "es_grupo": None},
        {"nombre_crudo": "Nombre", "ultima_fecha": "2026-11-05", "mensaje": "Texto privado"},
    ],
)
def test_rechaza_contacto_con_campos_o_tipos_incorrectos(
    tmp_path: Path, documento: dict[str, ValorJson], entrada: ValorJson
) -> None:
    ruta = tmp_path / "semana.json"
    documento["contactos"] = [entrada]
    guardar_json(ruta, documento)

    with pytest.raises(EstructuraError):
        LectorContactosJson(ruta).leer()


@pytest.mark.parametrize("campo", ["desde", "hasta", "ultima_fecha"])
@pytest.mark.parametrize("valor", [None, 20261105])
def test_fechas_deben_ser_texto_y_no_se_coercionan(
    tmp_path: Path, campo: str, valor: ValorJson
) -> None:
    ruta = tmp_path / "semana.json"
    rango: dict[str, ValorJson] = {"desde": "2026-11-02", "hasta": "2026-11-06"}
    contacto: dict[str, ValorJson] = {"nombre_crudo": "Nombre", "ultima_fecha": "2026-11-05"}
    if campo == "ultima_fecha":
        contacto[campo] = valor
    else:
        rango[campo] = valor
    guardar_json(ruta, {"rango": rango, "contactos": [contacto]})

    with pytest.raises(EstructuraError):
        LectorContactosJson(ruta).leer()


@pytest.mark.parametrize(
    "fecha",
    ["2026-02-30", "2026-11-05T12:00:00-03:00", "05/11/2026", "ayer", "20261105", "2026-W45-4"],
)
def test_rechaza_fechas_invalidas_o_distintas_de_yyyy_mm_dd(
    tmp_path: Path, documento: dict[str, ValorJson], fecha: str
) -> None:
    ruta = tmp_path / "semana.json"
    documento["contactos"] = [{"nombre_crudo": "Nombre", "ultima_fecha": fecha}]
    guardar_json(ruta, documento)

    with pytest.raises(ValidacionError):
        LectorContactosJson(ruta).leer()


def test_rechaza_periodo_invertido(tmp_path: Path, documento: dict[str, ValorJson]) -> None:
    ruta = tmp_path / "semana.json"
    documento["rango"] = {"desde": "2026-11-06", "hasta": "2026-11-02"}
    guardar_json(ruta, documento)

    with pytest.raises(ValidacionError, match="anterior"):
        LectorContactosJson(ruta).leer()


@pytest.mark.parametrize("fecha", ["2026-11-01", "2026-11-07"])
def test_contacto_fuera_del_periodo_no_se_descarta_silenciosamente(
    tmp_path: Path, documento: dict[str, ValorJson], fecha: str
) -> None:
    ruta = tmp_path / "semana.json"
    documento["contactos"] = [
        {"nombre_crudo": "Válido", "ultima_fecha": "2026-11-05"},
        {"nombre_crudo": "Fuera del período", "ultima_fecha": fecha},
    ]
    guardar_json(ruta, documento)

    with pytest.raises(ValidacionError, match="período"):
        LectorContactosJson(ruta).leer()


@pytest.mark.parametrize(
    "entrada",
    [
        {"nombre_crudo": " ", "ultima_fecha": "2026-11-05"},
        {"nombre_crudo": "Nombre", "ultima_fecha": "2026-11-05", "telefono": " "},
    ],
)
def test_rechaza_identidades_vacias(
    tmp_path: Path, documento: dict[str, ValorJson], entrada: ValorJson
) -> None:
    ruta = tmp_path / "semana.json"
    documento["contactos"] = [entrada]
    guardar_json(ruta, documento)

    with pytest.raises(ValidacionError):
        LectorContactosJson(ruta).leer()


@pytest.mark.parametrize("instante", [None, 123, False])
def test_metadato_de_extraccion_debe_ser_texto(
    tmp_path: Path, documento: dict[str, ValorJson], instante: ValorJson
) -> None:
    ruta = tmp_path / "semana.json"
    documento["extraido_en"] = instante
    guardar_json(ruta, documento)

    with pytest.raises(EstructuraError):
        LectorContactosJson(ruta).leer()


@pytest.mark.parametrize("instante", ["no es una fecha", "2026-11-05", "2026-11-05T12:00:00"])
def test_metadato_de_extraccion_requiere_instante_con_zona(
    tmp_path: Path, documento: dict[str, ValorJson], instante: str
) -> None:
    ruta = tmp_path / "semana.json"
    documento["extraido_en"] = instante
    guardar_json(ruta, documento)

    with pytest.raises(ValidacionError):
        LectorContactosJson(ruta).leer()


@pytest.mark.parametrize("instante", ["2026-11-06T21:04:00Z", "2026-11-06T18:04:00.123-03:00"])
def test_metadato_con_zona_es_valido_sin_modificar_fechas_del_chat(
    tmp_path: Path, documento: dict[str, ValorJson], instante: str
) -> None:
    ruta = tmp_path / "semana.json"
    documento["extraido_en"] = instante
    guardar_json(ruta, documento)

    assert LectorContactosJson(ruta).leer().contactos[0].ultima_fecha == date(2026, 11, 5)


@pytest.mark.parametrize("total", [None, True, 1.0, "812"])
def test_total_escaneado_requiere_entero_estricto(
    tmp_path: Path, documento: dict[str, ValorJson], total: ValorJson
) -> None:
    ruta = tmp_path / "semana.json"
    documento["total_chats_escaneados"] = total
    guardar_json(ruta, documento)

    with pytest.raises(EstructuraError):
        LectorContactosJson(ruta).leer()


@pytest.mark.parametrize("total", [-1, 0])
def test_total_escaneado_no_puede_ser_menor_que_contactos(
    tmp_path: Path, documento: dict[str, ValorJson], total: int
) -> None:
    ruta = tmp_path / "semana.json"
    documento["total_chats_escaneados"] = total
    guardar_json(ruta, documento)

    with pytest.raises(ValidacionError):
        LectorContactosJson(ruta).leer()


def test_archivo_ausente_no_se_interpreta_como_semana_vacia(tmp_path: Path) -> None:
    ruta = tmp_path / "nueva" / "semana.json"

    with pytest.raises(PersistenciaError) as capturada:
        LectorContactosJson(ruta).leer()

    assert isinstance(capturada.value.__cause__, FileNotFoundError)
    assert not ruta.parent.exists()


def test_error_de_acceso_se_traduce_sin_incluir_detalles_privados(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    causa = PermissionError("Ruta privada")
    mocker.patch.object(Path, "read_text", side_effect=causa)

    with pytest.raises(PersistenciaError) as capturada:
        LectorContactosJson(tmp_path / "semana.json").leer()

    assert capturada.value.__cause__ is causa
    assert "privada" not in str(capturada.value)


def test_log_de_exito_solo_incluye_cantidad_sin_identidades(
    tmp_path: Path, documento: dict[str, ValorJson], caplog: pytest.LogCaptureFixture
) -> None:
    ruta = tmp_path / "contacto-privado.json"
    guardar_json(ruta, documento)

    with caplog.at_level(logging.INFO):
        LectorContactosJson(ruta).leer()

    assert len(caplog.records) == 1
    assert "1 contactos" in caplog.text
    assert "privad" not in caplog.text
    assert "+5491100000000" not in caplog.text


def test_error_de_validacion_no_registra_contactos_parciales(
    tmp_path: Path, documento: dict[str, ValorJson], caplog: pytest.LogCaptureFixture
) -> None:
    ruta = tmp_path / "semana.json"
    documento["contactos"] = [
        {"nombre_crudo": "Nombre privado", "ultima_fecha": "2026-11-05"},
        {"nombre_crudo": "Nombre privado", "ultima_fecha": "2026-11-07"},
    ]
    guardar_json(ruta, documento)

    with caplog.at_level(logging.INFO), pytest.raises(ValidacionError) as capturada:
        LectorContactosJson(ruta).leer()

    assert not caplog.records
    assert "privad" not in str(capturada.value)
