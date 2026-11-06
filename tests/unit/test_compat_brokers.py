import pytest

from matching.brokers import TABLA_BROKERS_DEFAULT, extraer_brokers_y_nombre


def test_sin_parentesis_no_cambia_nada() -> None:
    nombre, hojas, ruido = extraer_brokers_y_nombre("Firuso Fivora")
    assert nombre == "Firuso Fivora"
    assert hojas == []
    assert ruido == []


def test_un_broker() -> None:
    nombre, hojas, ruido = extraer_brokers_y_nombre("Firuso Fivora (IOL)")
    assert nombre == "Firuso Fivora"
    assert hojas == ["IOL"]
    assert ruido == []


def test_dos_brokers_barra() -> None:
    _, hojas, _ = extraer_brokers_y_nombre("Firuso Fivora (IOL/Bull)")
    assert hojas == ["IOL", "Bull"]


def test_dos_brokers_con_y() -> None:
    _, hojas, _ = extraer_brokers_y_nombre("Firuso Fivora (IOL y Balanz)")
    assert hojas == ["IOL", "Balanz"]


def test_dos_brokers_con_guion() -> None:
    _, hojas, _ = extraer_brokers_y_nombre("Firuso Fivora (IOL - PPI)")
    assert hojas == ["IOL", "PPI"]


def test_variantes_de_nombre_de_broker() -> None:
    _, hojas, _ = extraer_brokers_y_nombre("Mizote Sivivu (invertironline)")
    assert hojas == ["IOL"]
    _, hojas, _ = extraer_brokers_y_nombre("Mizote Sivivu (Portfolio Personal)")
    assert hojas == ["PPI"]
    _, hojas, _ = extraer_brokers_y_nombre("Mizote Sivivu (BM)")
    assert hojas == ["Bull"]


def test_ruido_no_broker_no_rompe() -> None:
    nombre, hojas, ruido = extraer_brokers_y_nombre("Mizote Sivivu (hijo de Sivivu)")
    assert nombre == "Mizote Sivivu"
    assert hojas == []
    assert ruido == ["hijo de Sivivu"]


def test_broker_y_ruido_mezclados() -> None:
    _, hojas, ruido = extraer_brokers_y_nombre("Mizote Sivivu (IOL - cliente vip)")
    assert hojas == ["IOL"]
    assert ruido == ["cliente vip"]


def test_tabla_exportada_sigue_siendo_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(TABLA_BROKERS_DEFAULT, "personalizado", "OtraHoja")

    nombre, hojas, ruido = extraer_brokers_y_nombre("Contacto de prueba (personalizado)")

    assert nombre == "Contacto de prueba"
    assert hojas == ["OtraHoja"]
    assert ruido == []


def test_tabla_vacia_y_listas_del_formato_anterior_se_conservan() -> None:
    nombre, hojas, ruido = extraer_brokers_y_nombre("Contacto de prueba (IOL)", {})

    assert nombre == "Contacto de prueba"
    assert hojas == []
    assert ruido == ["IOL"]
