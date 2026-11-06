from indice.normalizacion import clave_orden, extraer_titulares, generar_claves, normalizar


def test_normalizar_minusculas_tildes_puntuacion() -> None:
    assert normalizar("Fédezu, Lídexe Bekaxo") == "fedezu lidexe bekaxo"


def test_normalizar_espacios_finales_y_multiples() -> None:
    assert normalizar("  PEXEZA   LEVOBA  ") == "pexeza levoba"


def test_clave_orden_ignora_formato_apellido_nombre() -> None:
    assert clave_orden("Zokasu, Sisopa") == clave_orden("ZOKASU, SISOPA")


def test_clave_orden_ignora_orden_de_tokens() -> None:
    assert clave_orden("Vurule, Lasise Guremo") == clave_orden("Lasise Guremo Vurule")


def test_generar_claves_conserva_crudo_intacto() -> None:
    crudo = "  fosata zepipa samame  "
    claves = generar_claves(crudo)
    assert claves["crudo"] == crudo
    assert claves["normalizado"] == "fosata zepipa samame"
    assert claves["clave_orden"] == "fosata samame zepipa"


def test_generar_claves_estilo_mayusculas_completo() -> None:
    claves = generar_claves("Goxane Fuzifa DELEVE")
    assert claves["normalizado"] == "goxane fuzifa deleve"


def test_titulares_caso_simple_no_se_rompe() -> None:
    assert extraer_titulares("Fedezu, Lidexe Bekaxo") == ["Fedezu, Lidexe Bekaxo"]


def test_titulares_y_o() -> None:
    assert extraer_titulares("RAXOZI GOGOTE Y/O RETUBO GEZIGE RATIXU") == [
        "RAXOZI GOGOTE",
        "RETUBO GEZIGE RATIXU",
    ]


def test_titulares_barra() -> None:
    assert extraer_titulares("RADONO XIVOVE/ NOGIMO VAFITU") == [
        "RADONO XIVOVE",
        "NOGIMO VAFITU",
    ]


def test_titulares_y_separador() -> None:
    assert extraer_titulares("VUVAPA BEKAXO SUBUMI Y VUVAPA GUSITA") == [
        "VUVAPA BEKAXO SUBUMI",
        "VUVAPA GUSITA",
    ]


def test_titulares_parentesis_esposa() -> None:
    assert extraer_titulares("MOPIVO BEMIBE (esposa Gosoru Fezixi)") == [
        "MOPIVO BEMIBE",
        "Gosoru Fezixi",
    ]


def test_titulares_parentesis_ruido_no_titular_se_descarta() -> None:
    # Contenido entre paréntesis sin prefijo de relación reconocido: no es titular.
    assert extraer_titulares("SIVIVU MIZOTE (cliente antiguo)") == ["SIVIVU MIZOTE"]


def test_mismo_clave_orden_para_titulares_equivalentes_en_distinto_formato() -> None:
    a = generar_claves("Gogote Raxozi")
    b = generar_claves("RAXOZI GOGOTE")
    assert a["clave_orden"] == b["clave_orden"]
