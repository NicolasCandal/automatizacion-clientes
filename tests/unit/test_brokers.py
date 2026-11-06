"""Broker recognition and preservation of extraction rules."""

from dataclasses import FrozenInstanceError
from types import MappingProxyType

import pytest

from client_automation.core.brokers import extract_brokers_and_name


@pytest.mark.parametrize(
    ("alias", "sheet"),
    [
        ("IOL", "IOL"),
        ("invertironline", "IOL"),
        ("I\u00d3L", "IOL"),
        ("bull", "Bull"),
        ("BM", "Bull"),
        ("BULL MARKET", "Bull"),
        ("Balanz", "Balanz"),
        ("PPI", "PPI"),
        ("Portfolio Personal", "PPI"),
        ("Inviu", "Inviu"),
    ],
)
def test_alias_identifies_one_sheet_canonical(alias: str, sheet: str) -> None:
    result = extract_brokers_and_name(f"Sample contact ({alias})")

    assert result.name_without_parentheses == "Sample contact"
    assert result.sheets == (sheet,)
    assert result.noise == ()


@pytest.mark.parametrize(
    ("content", "sheets"),
    [
        ("IOL/Bull", ("IOL", "Bull")),
        ("IOL and Balanz", ("IOL", "Balanz")),
        ("IOL AND PPI", ("IOL", "PPI")),
        ("IOL - PPI", ("IOL", "PPI")),
        ("IOL, Inviu", ("IOL", "Inviu")),
        ("/IOL//Bull/", ("IOL", "Bull")),
        ("Bull/IOL and PPI, Inviu - Balanz", ("Bull", "IOL", "PPI", "Inviu", "Balanz")),
    ],
)
def test_separators_allow_multiple_sheets_in_order(content: str, sheets: tuple[str, ...]) -> None:
    result = extract_brokers_and_name(f"Sample contact ({content})")

    assert result.sheets == sheets
    assert result.noise == ()


def test_sheets_repeated_by_alias_or_parentheses_no_duplicate() -> None:
    result = extract_brokers_and_name(
        "Sample contact (Bull/IOL) (BM/invertironline/PPI/Bull Market)"
    )

    assert result.name_without_parentheses == "Sample contact"
    assert result.sheets == ("Bull", "IOL", "PPI")


def test_noise_preserves_text_original_and_repetitions() -> None:
    result = extract_brokers_and_name("Sample contact (IOL - Cl\u00edent VIP, Other/Other)")

    assert result.sheets == ("IOL",)
    assert result.noise == ("Cl\u00edent VIP", "Other", "Other")


def test_relationship_no_converts_in_broker() -> None:
    result = extract_brokers_and_name("Sample contact (son of Other Person)")

    assert result.sheets == ()
    assert result.noise == ("son of Other Person",)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("", ""),
        (" \t\n ", ""),
        ("  SAMPLE, R\u00e1dono\tM\u00edzafe  ", "SAMPLE, R\u00e1dono M\u00edzafe"),
        ("Contact IOL", "Contact IOL"),
        ("Contact (IOL", "Contact (IOL"),
        ("Contact IOL)", "Contact IOL)"),
        ("Contact ()", "Contact"),
    ],
)
def test_name_preserves_without_infer_brokers_outside_of_parentheses(
    name: str, expected: str
) -> None:
    result = extract_brokers_and_name(name)

    assert result.name_without_parentheses == expected
    assert result.sheets == ()
    assert result.noise == ()


def test_name_without_text_can_preserve_sheets_recognized() -> None:
    result = extract_brokers_and_name("(IOL)")

    assert result.name_without_parentheses == ""
    assert result.sheets == ("IOL",)


def test_table_custom_replaces_the_default_without_mutate() -> None:
    table = {"special": "OtherSheet"}
    result = extract_brokers_and_name("Contact (special/IOL)", table)

    assert result.sheets == ("OtherSheet",)
    assert result.noise == ("IOL",)
    assert table == {"special": "OtherSheet"}
    assert extract_brokers_and_name("Contact (special)").sheets == ()


def test_table_empty_disables_the_recognition() -> None:
    result = extract_brokers_and_name("Contact (IOL/Bull)", {})

    assert result.sheets == ()
    assert result.noise == ("IOL", "Bull")


def test_table_only_read_is_compatible_and_uses_keys_normalized() -> None:
    table = MappingProxyType({"my broker": "OtherSheet"})
    result = extract_brokers_and_name("Contact (M\u00dd BROKER)", table)

    assert result.sheets == ("OtherSheet",)
    assert result.noise == ()


def test_result_is_immutable_and_no_exposes_name_or_noise() -> None:
    result = extract_brokers_and_name("Sample contact (IOL, Note private)")

    assert "Sample contact" not in repr(result)
    assert "Note private" not in repr(result)
    with pytest.raises(FrozenInstanceError):
        result.__setattr__("sheets", ("Bull",))
