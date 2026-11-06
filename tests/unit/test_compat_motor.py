from pathlib import Path

import pytest

from automatizacion_clientes.infra.formatos_compatibilidad import (
    AparicionHistorica,
    CatalogoHistorico,
    PersonaHistorica,
    ResultadoHistorico,
)
from matching.alias_store import AliasStore
from matching.lista_negra import ListaNegra
from matching.motor import construir_indice, resolver_contacto, resolver_semana


def _persona(
    id_: str,
    clave_orden: str,
    nombre_canonico: str,
    titulares: list[str],
    apariciones: list[AparicionHistorica],
) -> PersonaHistorica:
    return {
        "id": id_,
        "clave_orden": clave_orden,
        "nombre_canonico": nombre_canonico,
        "titulares": titulares,
        "apariciones": apariciones,
    }


def _catalogo_base() -> CatalogoHistorico:
    return {
        "personas": [
            _persona(
                "p0001",
                "baleve firuso fivora",
                "Firuso Baleve Fivora",
                ["Firuso Baleve Fivora"],
                [{"hoja": "IOL", "fila": 27, "nombre_crudo": "Firuso Baleve Fivora"}],
            ),
            _persona(
                "p0002",
                "guremo lasise vurule",
                "Vurule, Lasise Guremo",
                ["Vurule, Lasise Guremo"],
                [{"hoja": "Bull", "fila": 10, "nombre_crudo": "Vurule, Lasise Guremo"}],
            ),
            _persona(
                "p0003",
                "sisopa zokasu",
                "Zokasu, Sisopa",
                ["Zokasu, Sisopa"],
                [{"hoja": "IOL", "fila": 55, "nombre_crudo": "Zokasu, Sisopa"}],
            ),
            _persona(
                "p0004",
                "gogote raxozi",
                "RAXOZI GOGOTE",
                ["RAXOZI GOGOTE"],
                [
                    {
                        "hoja": "Balanz",
                        "fila": 4,
                        "nombre_crudo": "RAXOZI GOGOTE Y/O RETUBO GEZIGE RATIXU",
                    }
                ],
            ),
            _persona(
                "p0005",
                "gezige ratixu retubo",
                "RETUBO GEZIGE RATIXU",
                ["RETUBO GEZIGE RATIXU"],
                [
                    {
                        "hoja": "Balanz",
                        "fila": 4,
                        "nombre_crudo": "RAXOZI GOGOTE Y/O RETUBO GEZIGE RATIXU",
                    }
                ],
            ),
            # Dos personas distintas que ambas matchean "Mizote Sivivu" por fuzzy
            # (token_set_ratio: el query es subconjunto de las dos -> 100 y 100)
            _persona(
                "p0006",
                "kukeve mizote sivivu",
                "Mizote Sivivu Kukeve",
                ["Mizote Sivivu Kukeve"],
                [{"hoja": "IOL", "fila": 100, "nombre_crudo": "Mizote Sivivu Kukeve"}],
            ),
            _persona(
                "p0007",
                "gibelo mizote sivivu",
                "Mizote Sivivu Gibelo",
                ["Mizote Sivivu Gibelo"],
                [{"hoja": "Bull", "fila": 200, "nombre_crudo": "Mizote Sivivu Gibelo"}],
            ),
        ]
    }


@pytest.fixture
def catalogo() -> CatalogoHistorico:
    return _catalogo_base()


@pytest.fixture
def alias_store(tmp_path: Path) -> AliasStore:
    return AliasStore(tmp_path / "aliases.json")


@pytest.fixture
def lista_negra(tmp_path: Path) -> ListaNegra:
    return ListaNegra(tmp_path / "lista_negra.json")


def _resolver_uno(
    nombre_crudo: str,
    catalogo: CatalogoHistorico,
    alias_store: AliasStore,
    lista_negra: ListaNegra,
    telefono: str | None = None,
    es_grupo: bool = False,
) -> list[ResultadoHistorico]:
    idx = construir_indice(catalogo)
    contacto = {
        "nombre_crudo": nombre_crudo,
        "ultima_fecha": "2026-07-20",
        "telefono": telefono,
        "es_grupo": es_grupo,
    }
    return resolver_contacto(contacto, idx, alias_store, lista_negra)


def test_exacto_con_broker_correcto(
    catalogo: CatalogoHistorico, alias_store: AliasStore, lista_negra: ListaNegra
) -> None:
    resultados = _resolver_uno("Firuso Baleve Fivora (IOL)", catalogo, alias_store, lista_negra)
    assert len(resultados) == 1
    r = resultados[0]
    assert r["tipo"] == "resuelto"
    assert r["persona_id"] == "p0001"
    assert r["via"] == "exacto"
    assert r["hojas"] == ["IOL"]
    assert r["fila_por_hoja"] == {"IOL": 27}


def test_formato_apellido_coma_nombre_matchea_igual(
    catalogo: CatalogoHistorico, alias_store: AliasStore, lista_negra: ListaNegra
) -> None:
    resultados = _resolver_uno("ZOKASU, SISOPA (IOL)", catalogo, alias_store, lista_negra)
    assert resultados[0]["persona_id"] == "p0003"
    assert resultados[0]["via"] == "exacto"


def test_orden_de_nombre_y_apellido_invertido(
    catalogo: CatalogoHistorico, alias_store: AliasStore, lista_negra: ListaNegra
) -> None:
    resultados = _resolver_uno("Lasise Guremo Vurule (Bull)", catalogo, alias_store, lista_negra)
    assert resultados[0]["persona_id"] == "p0002"
    assert resultados[0]["via"] == "exacto"


def test_titularidad_compartida_matchea_por_un_solo_titular(
    catalogo: CatalogoHistorico, alias_store: AliasStore, lista_negra: ListaNegra
) -> None:
    resultados = _resolver_uno("Gogote Raxozi (Balanz)", catalogo, alias_store, lista_negra)
    assert len(resultados) == 1
    assert resultados[0]["persona_id"] == "p0004"
    assert resultados[0]["hojas"] == ["Balanz"]
    assert resultados[0]["fila_por_hoja"] == {"Balanz": 4}


def test_fuzzy_ignora_segundo_nombre_de_mas(
    catalogo: CatalogoHistorico, alias_store: AliasStore, lista_negra: ListaNegra
) -> None:
    # "firuso fivora" vs "Firuso Baleve Fivora": token_set_ratio da 100 pese al
    # token de más, mientras que un ratio simple daría ~75.
    resultados = _resolver_uno("Firuso Fivora (IOL)", catalogo, alias_store, lista_negra)
    assert resultados[0]["tipo"] == "resuelto"
    assert resultados[0]["persona_id"] == "p0001"
    assert resultados[0]["via"] == "fuzzy"
    assert resultados[0]["score"] == 100


def test_broker_no_coincide_va_a_revision_aunque_nombre_de_100(
    catalogo: CatalogoHistorico, alias_store: AliasStore, lista_negra: ListaNegra
) -> None:
    # Firuso Baleve Fivora solo está en IOL; si el contacto dice Bull, no autoresuelve.
    resultados = _resolver_uno("Firuso Baleve Fivora (Bull)", catalogo, alias_store, lista_negra)
    assert resultados[0]["tipo"] == "pendiente"
    assert resultados[0]["motivo"] == "broker_no_coincide"
    assert resultados[0]["candidatos"][0]["persona_id"] == "p0001"


def test_ambiguo_dos_candidatos_con_100(
    catalogo: CatalogoHistorico, alias_store: AliasStore, lista_negra: ListaNegra
) -> None:
    resultados = _resolver_uno("Mizote Sivivu", catalogo, alias_store, lista_negra)
    assert resultados[0]["tipo"] == "pendiente"
    assert resultados[0]["motivo"] == "ambiguo"
    ids_con_100 = {c["persona_id"] for c in resultados[0]["candidatos"] if c["score"] == 100}
    assert ids_con_100 == {"p0006", "p0007"}


def test_sin_match_cliente_nuevo(
    catalogo: CatalogoHistorico, alias_store: AliasStore, lista_negra: ListaNegra
) -> None:
    resultados = _resolver_uno("Tevosi Sinapellido Desconocido", catalogo, alias_store, lista_negra)
    assert resultados[0]["tipo"] == "pendiente"
    assert resultados[0]["motivo"] == "sin_match"


def test_grupo_se_ignora(
    catalogo: CatalogoHistorico, alias_store: AliasStore, lista_negra: ListaNegra
) -> None:
    resultados = _resolver_uno("Grupo Familia", catalogo, alias_store, lista_negra, es_grupo=True)
    assert resultados[0]["tipo"] == "ignorado"
    assert resultados[0]["motivo"] == "grupo"


def test_lista_negra_se_ignora(
    catalogo: CatalogoHistorico, alias_store: AliasStore, lista_negra: ListaNegra
) -> None:
    lista_negra.agregar("El Contador")
    resultados = _resolver_uno("El Contador", catalogo, alias_store, lista_negra)
    assert resultados[0]["tipo"] == "ignorado"
    assert resultados[0]["motivo"] == "en_lista_negra"


def test_resuelto_se_persiste_en_alias_store(
    catalogo: CatalogoHistorico, alias_store: AliasStore, lista_negra: ListaNegra
) -> None:
    _resolver_uno("Firuso Baleve Fivora (IOL)", catalogo, alias_store, lista_negra)
    entrada = alias_store.buscar_por_nombre("Firuso Baleve Fivora (IOL)")
    assert entrada is not None
    assert entrada["personas"] == [{"persona_id": "p0001", "hojas": ["IOL"]}]


def test_segunda_vez_resuelve_por_alias_sin_recalcular(
    catalogo: CatalogoHistorico, alias_store: AliasStore, lista_negra: ListaNegra
) -> None:
    _resolver_uno("Firuso Baleve Fivora (IOL)", catalogo, alias_store, lista_negra)
    # Ahora un nombre que NO matchearía por exacto/fuzzy, pero SÍ está en el alias store
    alias_store.registrar("Apodo Raro", [{"persona_id": "p0001", "hojas": ["IOL"]}])
    resultados = _resolver_uno("Apodo Raro", catalogo, alias_store, lista_negra)
    assert resultados[0]["tipo"] == "resuelto"
    assert resultados[0]["via"] == "alias"
    assert resultados[0]["persona_id"] == "p0001"


def test_telefono_resuelve_aunque_nombre_no_matchee(
    catalogo: CatalogoHistorico, alias_store: AliasStore, lista_negra: ListaNegra
) -> None:
    alias_store.registrar(
        "Firuso IOL viejo", [{"persona_id": "p0001", "hojas": ["IOL"]}], "+5491155551234"
    )
    resultados = _resolver_uno(
        "Nombre Completamente Distinto",
        catalogo,
        alias_store,
        lista_negra,
        telefono="+5491155551234",
    )
    assert resultados[0]["tipo"] == "resuelto"
    assert resultados[0]["via"] == "telefono"
    assert resultados[0]["persona_id"] == "p0001"


def test_alias_no_se_sobrescribe_en_silencio(
    catalogo: CatalogoHistorico, alias_store: AliasStore, lista_negra: ListaNegra
) -> None:
    alias_store.registrar("Contacto X", [{"persona_id": "p0001", "hojas": ["IOL"]}])
    aviso = alias_store.registrar("Contacto X", [{"persona_id": "p0099", "hojas": ["Bull"]}])
    assert aviso is not None
    # no se pisó
    conservado = alias_store.buscar_por_nombre("Contacto X")
    assert conservado is not None
    assert conservado["personas"] == [{"persona_id": "p0001", "hojas": ["IOL"]}]


def test_resolver_semana_agrupa_correctamente(
    catalogo: CatalogoHistorico, alias_store: AliasStore, lista_negra: ListaNegra
) -> None:
    contactos = [
        {
            "nombre_crudo": "Firuso Baleve Fivora (IOL)",
            "ultima_fecha": "2026-07-20",
            "telefono": None,
            "es_grupo": False,
        },
        {
            "nombre_crudo": "Tevosi Desconocido",
            "ultima_fecha": "2026-07-20",
            "telefono": None,
            "es_grupo": False,
        },
        {
            "nombre_crudo": "Grupo Amigos",
            "ultima_fecha": "2026-07-20",
            "telefono": None,
            "es_grupo": True,
        },
    ]
    resolucion = resolver_semana(contactos, catalogo, alias_store, lista_negra)
    assert len(resolucion["resueltos"]) == 1
    assert len(resolucion["pendientes"]) == 1
    assert len(resolucion["ignorados"]) == 1
