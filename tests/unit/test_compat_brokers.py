import pytest

from matching.brokers import DEFAULT_BROKER_TABLE, extract_brokers_and_name


def test_without_parentheses_no_changes_nothing() -> None:
    name, sheets, noise = extract_brokers_and_name("Firuso Fivora")
    assert name == "Firuso Fivora"
    assert sheets == []
    assert noise == []


def test_one_broker() -> None:
    name, sheets, noise = extract_brokers_and_name("Firuso Fivora (IOL)")
    assert name == "Firuso Fivora"
    assert sheets == ["IOL"]
    assert noise == []


def test_two_brokers_slash() -> None:
    _, sheets, _ = extract_brokers_and_name("Firuso Fivora (IOL/Bull)")
    assert sheets == ["IOL", "Bull"]


def test_two_brokers_with_and() -> None:
    _, sheets, _ = extract_brokers_and_name("Firuso Fivora (IOL and Balanz)")
    assert sheets == ["IOL", "Balanz"]


def test_two_brokers_with_hyphen() -> None:
    _, sheets, _ = extract_brokers_and_name("Firuso Fivora (IOL - PPI)")
    assert sheets == ["IOL", "PPI"]


def test_variants_of_name_of_broker() -> None:
    _, sheets, _ = extract_brokers_and_name("Mizote Sivivu (invertironline)")
    assert sheets == ["IOL"]
    _, sheets, _ = extract_brokers_and_name("Mizote Sivivu (Portfolio Personal)")
    assert sheets == ["PPI"]
    _, sheets, _ = extract_brokers_and_name("Mizote Sivivu (BM)")
    assert sheets == ["Bull"]


def test_noise_no_broker_no_breaks() -> None:
    name, sheets, noise = extract_brokers_and_name("Mizote Sivivu (son of Sivivu)")
    assert name == "Mizote Sivivu"
    assert sheets == []
    assert noise == ["son of Sivivu"]


def test_broker_and_noise_mixed() -> None:
    _, sheets, noise = extract_brokers_and_name("Mizote Sivivu (IOL - client vip)")
    assert sheets == ["IOL"]
    assert noise == ["client vip"]


def test_table_exported_remains_being_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(DEFAULT_BROKER_TABLE, "custom", "OtherSheet")

    name, sheets, noise = extract_brokers_and_name("Sample contact (custom)")

    assert name == "Sample contact"
    assert sheets == ["OtherSheet"]
    assert noise == []


def test_table_empty_and_lists_of_format_previous_preserve() -> None:
    name, sheets, noise = extract_brokers_and_name("Sample contact (IOL)", {})

    assert name == "Sample contact"
    assert sheets == []
    assert noise == ["IOL"]
