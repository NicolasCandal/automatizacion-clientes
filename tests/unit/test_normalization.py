"""Name normalization rules without external dependencies."""

from dataclasses import FrozenInstanceError

import pytest

from client_automation.core.normalization import (
    extract_holders,
    generate_name_keys,
    normalize,
    sorted_name_key,
    strip_accents,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", ""),
        (" \n\t ", ""),
        ("--- / () !!!", ""),
        ("F\u00e9dezu, L\u00eddexe Bekaxo", "fedezu lidexe bekaxo"),
        ("  PEXEZA   LEVOBA  ", "pexeza levoba"),
        ("S\u00e1bili", "sabili"),
        ("Nin\u0303o", "nino"),
        ("R\u00e1dono\t\u00a0Sample\nZeta", "radono sample zeta"),
        ("Text-One O'Filter", "text one o filter"),
        ("D'\u00c1baco & Sample-Zeta", "d abaco sample zeta"),
        ("CLIENT_123", "client_123"),
        ("M\u00edzafe (IOL)", "mizafe iol"),
        ("Stra\u00dfe", "stra\u00dfe"),
        ("\uff23\uff2c\uff29\uff25\uff2e\uff34", "client"),
        ("\ufb02ow", "flow"),
        ("\u0301", ""),
    ],
)
def test_normalize_preserves_the_rules_of_comparison(text: str, expected: str) -> None:
    assert normalize(text) == expected


def test_strip_accents_preserves_uppercase_and_spaces() -> None:
    assert strip_accents("  \u00c1\u00c9\u00cd\u00d3\u00da \u00d1 \ufb02  ") == "  AEIOU N fl  "
    assert strip_accents("") == ""


@pytest.mark.parametrize(
    ("name", "variant"),
    [
        ("Zokasu, Sisopa", "ZOKASU, SISOPA"),
        ("Vurule, Lasise Guremo", "Lasise Guremo Vurule"),
        ("Gogote Raxozi", "RAXOZI GOGOTE"),
        ("Sample, R\u00e1dono M\u00edzafe", "Mizafe Radono Sample"),
    ],
)
def test_key_order_identifies_variants_of_name(name: str, variant: str) -> None:
    assert sorted_name_key(name) == sorted_name_key(variant)


def test_key_order_no_discards_words_repeated() -> None:
    assert sorted_name_key("Axa Axa Sample") == "axa axa sample"
    assert sorted_name_key("Axa Axa Sample") != sorted_name_key("Axa Sample")
    assert sorted_name_key("") == ""


def test_generate_keys_preserves_original_and_returns_one_model_immutable() -> None:
    original = "  SAMPLE, R\u00e1dono M\u00edzafe  "
    keys = generate_name_keys(original)

    assert keys.raw == original
    assert keys.normalized == "sample radono mizafe"
    assert keys.sorted_name_key == "mizafe radono sample"
    assert original not in repr(keys)
    assert keys.normalized not in repr(keys)
    assert keys.sorted_name_key not in repr(keys)
    with pytest.raises(FrozenInstanceError):
        keys.__setattr__("raw", "Other name")


def test_generate_keys_accepts_input_without_name() -> None:
    keys = generate_name_keys("  ")

    assert keys.raw == "  "
    assert keys.normalized == ""
    assert keys.sorted_name_key == ""


def test_order_of_operations_unicode_no_changes_keys_legacy() -> None:
    # NFKD converts this modifier character to uppercase A; retain the historical
    # second normalization pass when generating the sorted name key.
    keys = generate_name_keys("\u1d2c")

    assert keys.normalized == "A"
    assert keys.sorted_name_key == "a"


@pytest.mark.parametrize(
    ("name", "holders"),
    [
        ("", ()),
        (" \n ", ()),
        ("Fedezu, Lidexe Bekaxo", ("Fedezu, Lidexe Bekaxo",)),
        ("  Sample, R\u00e1dono  ", ("Sample, R\u00e1dono",)),
        (
            "RAXOZI GOGOTE AND/OR RETUBO GEZIGE RATIXU",
            ("RAXOZI GOGOTE", "RETUBO GEZIGE RATIXU"),
        ),
        ("Axa and/or Daresu and/or Fesazo", ("Axa", "Daresu", "Fesazo")),
        ("RADONO XIVOVE/ NOGIMO VAFITU", ("RADONO XIVOVE", "NOGIMO VAFITU")),
        ("/ Axa // Daresu /", ("Axa", "Daresu")),
        ("Axa AND Daresu", ("Axa", "Daresu")),
        ("Axa and Daresu and Fesazo", ("Axa and Daresu and Fesazo",)),
        ("Axa AND/OR Daresu/Fesazo", ("Axa", "Daresu/Fesazo")),
        ("Axa/Daresu and Fesazo", ("Axa", "Daresu and Fesazo")),
        ("Axa/Axa", ("Axa", "Axa")),
        ("Axa (client old)", ("Axa",)),
        ("Axa ()", ("Axa",)),
        ("(IOL)", ()),
        ("Axa (wife Daresu)", ("Axa", "Daresu")),
        ("Axa (HUSBAND OF Daresu)", ("Axa", "Daresu")),
        ("Axa (son of Daresu)", ("Axa", "Daresu")),
        ("Axa (daughter Fesazo)", ("Axa", "Fesazo")),
        ("Axa (son)", ("Axa",)),
        ("(daughter of Fesazo)", ("Fesazo",)),
        ("Axa (IOL) (wife Daresu)", ("Axa  (wife Daresu)",)),
        ("Axa (wife Daresu) (IOL)", ("Axa  (IOL)", "Daresu")),
        ("Axa (wife Daresu", ("Axa (wife Daresu",)),
    ],
)
def test_extract_holders_preserves_priorities_and_heuristic(
    name: str, holders: tuple[str, ...]
) -> None:
    assert extract_holders(name) == holders


def test_extraction_preserves_format_of_the_names_and_prefix_family() -> None:
    original = "  SAMPLE, R\u00e1dono / ZETA, M\u00edzafe (wife Gosoru)  "

    assert extract_holders(original) == ("SAMPLE, R\u00e1dono", "ZETA, M\u00edzafe", "Gosoru")
