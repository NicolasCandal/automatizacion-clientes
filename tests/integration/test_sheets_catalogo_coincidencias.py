"""API simulada de Sheets conectada al catálogo y a los resultados JSON reales."""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest
from pytest_mock import MockerFixture
from requests import Response

from automatizacion_clientes.core.errores import ConcurrenciaError
from automatizacion_clientes.core.modelos import UbicacionCliente
from automatizacion_clientes.infra.almacen_aliases_json import AlmacenAliasesJson
from automatizacion_clientes.infra.almacen_catalogo_json import AlmacenCatalogoJson
from automatizacion_clientes.infra.almacen_resoluciones_json import AlmacenResolucionesJson
from automatizacion_clientes.infra.almacenamiento_json import guardar_json
from automatizacion_clientes.infra.comparador_rapidfuzz import ComparadorRapidFuzz
from automatizacion_clientes.infra.lector_clientes_sheets import LectorClientesSheets
from automatizacion_clientes.infra.lector_contactos_json import LectorContactosJson
from automatizacion_clientes.infra.lista_exclusion_json import ListaExclusionJson
from automatizacion_clientes.infra.reloj_sistema import RelojSistema
from automatizacion_clientes.services.catalogo import ServicioCatalogo
from automatizacion_clientes.services.coincidencias import ServicioCoincidencias


class SesionSimulada:
    def __init__(self) -> None:
        hojas: list[dict[str, object]] = []
        for indice, nombre in enumerate(("Inviu", "IOL", "Bull", "Balanz", "PPI")):
            columna = 4 if nombre == "Balanz" else 3
            encabezados: list[object] = [{} for _ in range(columna - 1)]
            encabezados.append({"userEnteredValue": {"stringValue": "Semana"}})
            hojas.append(
                {
                    "properties": {
                        "title": nombre,
                        "sheetId": indice,
                        "gridProperties": {"rowCount": 100, "columnCount": 10},
                    },
                    "data": [
                        {
                            "rowData": [
                                {"values": [{"userEnteredValue": {"stringValue": "Cliente"}}]},
                                {"values": encabezados},
                                {"values": [{"userEnteredValue": {"stringValue": "Axa Prueba"}}]},
                                {},
                                {
                                    "values": [
                                        {
                                            "userEnteredValue": {
                                                "stringValue": "Prueba, Axa"
                                                if nombre == "IOL"
                                                else "Notuka Ejemplo"
                                            }
                                        }
                                    ]
                                },
                            ]
                        }
                    ],
                }
            )
        self.documento: dict[str, object] = {
            "spreadsheetId": "documento_sintetico",
            "sheets": hojas,
        }

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str | list[str]],
        timeout: float,
        allow_redirects: bool,
    ) -> Response:
        respuesta = Response()
        respuesta.status_code = 200
        respuesta.encoding = "utf-8"
        respuesta._content = json.dumps(self.documento).encode("utf-8")
        return respuesta

    def close(self) -> None:
        pass


def test_sheets_alimenta_catalogo_y_coincidencias_con_todas_las_filas(tmp_path: Path) -> None:
    sesion = SesionSimulada()
    catalogos = AlmacenCatalogoJson(tmp_path / "catalogo.json")
    with LectorClientesSheets("documento_sintetico", sesion=sesion) as lector:
        servicio = ServicioCatalogo(
            lector_clientes=lector,
            almacen_catalogo=catalogos,
            reloj=RelojSistema(),
            identificador_origen="sheets_sintetico",
        )
        catalogo = servicio.ejecutar(simulacion=False)
        assert len(catalogo.personas) == 2
        assert catalogos.leer() == catalogo
        assert servicio.ejecutar() == catalogo
    contactos_path = tmp_path / "semana.json"
    guardar_json(
        contactos_path,
        {
            "rango": {"desde": "2026-11-02", "hasta": "2026-11-06"},
            "contactos": [
                {"nombre_crudo": "Axa Prueba (IOL/Balanz)", "ultima_fecha": "2026-11-05"}
            ],
        },
    )
    informe = AlmacenResolucionesJson(tmp_path / "informe.json")
    coincidencias = ServicioCoincidencias(
        lector_contactos=LectorContactosJson(contactos_path),
        almacen_catalogo=catalogos,
        almacen_aliases=AlmacenAliasesJson(tmp_path / "aliases.json"),
        lista_exclusion=ListaExclusionJson(tmp_path / "exclusiones.json"),
        almacen_resoluciones=informe,
        comparador=ComparadorRapidFuzz(),
    )
    resultado = coincidencias.ejecutar(simulacion=False)
    assert len(resultado.resueltos) == 1
    assert resultado.resueltos[0].ubicaciones == (
        UbicacionCliente(hoja="IOL", fila=3),
        UbicacionCliente(hoja="IOL", fila=5),
        UbicacionCliente(hoja="Balanz", fila=3),
    )
    assert informe.leer() == resultado


def test_cambio_de_sheets_durante_lectura_conserva_catalogo_anterior(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    sesion = SesionSimulada()
    ruta = tmp_path / "catalogo.json"
    with LectorClientesSheets("documento_sintetico", sesion=sesion) as lector:
        servicio = ServicioCatalogo(
            lector_clientes=lector,
            almacen_catalogo=AlmacenCatalogoJson(ruta),
            reloj=RelojSistema(),
            identificador_origen="sheets_sintetico",
        )
        servicio.ejecutar(simulacion=False)
        original = ruta.read_bytes()
        consultar = sesion.get
        llamadas = 0

        def consultar_con_cambio(
            url: str,
            *,
            params: Mapping[str, str | list[str]],
            timeout: float,
            allow_redirects: bool,
        ) -> Response:
            nonlocal llamadas
            llamadas += 1
            if llamadas == 2:
                hojas = cast(list[dict[str, object]], sesion.documento["sheets"])
                hojas[1]["bandedRanges"] = [{"bandedRangeId": 123}]
            return consultar(url, params=params, timeout=timeout, allow_redirects=allow_redirects)

        mocker.patch.object(sesion, "get", side_effect=consultar_con_cambio)
        with pytest.raises(ConcurrenciaError):
            servicio.ejecutar(simulacion=False, forzar=True)
    assert ruta.read_bytes() == original
