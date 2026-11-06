from catalog_index.normalization import (
    extract_holders,
    generate_name_keys,
    normalize,
    sorted_name_key,
)


def test_normalize_lowercase_accents_score() -> None:
    assert normalize("F\u00e9dezu, L\u00eddexe Bekaxo") == "fedezu lidexe bekaxo"


def test_normalize_spaces_trailing_and_multiple() -> None:
    assert normalize("  PEXEZA   LEVOBA  ") == "pexeza levoba"


def test_key_order_ignores_format_surname_name() -> None:
    assert sorted_name_key("Zokasu, Sisopa") == sorted_name_key("ZOKASU, SISOPA")


def test_key_order_ignores_order_of_tokens() -> None:
    assert sorted_name_key("Vurule, Lasise Guremo") == sorted_name_key("Lasise Guremo Vurule")


def test_generate_keys_preserves_raw_unchanged() -> None:
    raw = "  fosata zepipa samame  "
    keys = generate_name_keys(raw)
    assert keys["raw"] == raw
    assert keys["normalized"] == "fosata zepipa samame"
    assert keys["sorted_name_key"] == "fosata samame zepipa"


def test_generate_keys_style_uppercase_complete() -> None:
    keys = generate_name_keys("Goxane Fuzifa DELEVE")
    assert keys["normalized"] == "goxane fuzifa deleve"


def test_holders_case_simple_no_breaks() -> None:
    assert extract_holders("Fedezu, Lidexe Bekaxo") == ["Fedezu, Lidexe Bekaxo"]


def test_holders_and_or() -> None:
    assert extract_holders("RAXOZI GOGOTE AND/OR RETUBO GEZIGE RATIXU") == [
        "RAXOZI GOGOTE",
        "RETUBO GEZIGE RATIXU",
    ]


def test_holders_slash() -> None:
    assert extract_holders("RADONO XIVOVE/ NOGIMO VAFITU") == [
        "RADONO XIVOVE",
        "NOGIMO VAFITU",
    ]


def test_holders_and_separator() -> None:
    assert extract_holders("VUVAPA BEKAXO SUBUMI AND VUVAPA GUSITA") == [
        "VUVAPA BEKAXO SUBUMI",
        "VUVAPA GUSITA",
    ]


def test_holders_parentheses_wife() -> None:
    assert extract_holders("MOPIVO BEMIBE (wife Gosoru Fezixi)") == [
        "MOPIVO BEMIBE",
        "Gosoru Fezixi",
    ]


def test_holders_parentheses_noise_no_holder_discards() -> None:
    # Parenthesized noise without a recognized relationship prefix is not a holder.
    assert extract_holders("SIVIVU MIZOTE (client old)") == ["SIVIVU MIZOTE"]


def test_same_key_order_for_holders_equivalent_in_different_format() -> None:
    a = generate_name_keys("Gogote Raxozi")
    b = generate_name_keys("RAXOZI GOGOTE")
    assert a["sorted_name_key"] == b["sorted_name_key"]
