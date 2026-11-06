"""Mock Sheets API connected to the catalog and real JSON result adapters."""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest
from pytest_mock import MockerFixture
from requests import Response

from client_automation.core.errors import ConcurrencyError
from client_automation.core.models import ClientLocation
from client_automation.infra.json_alias_store import JsonAliasStore
from client_automation.infra.json_catalog_store import JsonCatalogStore
from client_automation.infra.json_contact_reader import JsonContactReader
from client_automation.infra.json_exclusion_list import JsonExclusionList
from client_automation.infra.json_resolution_store import JsonResolutionStore
from client_automation.infra.json_storage import save_json
from client_automation.infra.rapidfuzz_comparator import RapidFuzzComparator
from client_automation.infra.sheets_client_reader import SheetsClientReader
from client_automation.infra.system_clock import SystemClock
from client_automation.services.catalog import CatalogService
from client_automation.services.matching import MatchingService


class FakeTransport:
    def __init__(self) -> None:
        sheets: list[dict[str, object]] = []
        for catalog_index, name in enumerate(("Inviu", "IOL", "Bull", "Balanz", "PPI")):
            column = 4 if name == "Balanz" else 3
            headers: list[object] = [{} for _ in range(column - 1)]
            headers.append({"userEnteredValue": {"stringValue": "Week"}})
            sheets.append(
                {
                    "properties": {
                        "title": name,
                        "sheetId": catalog_index,
                        "gridProperties": {"rowCount": 100, "columnCount": 10},
                    },
                    "data": [
                        {
                            "rowData": [
                                {"values": [{"userEnteredValue": {"stringValue": "Client"}}]},
                                {"values": headers},
                                {"values": [{"userEnteredValue": {"stringValue": "Axa Sample"}}]},
                                {},
                                {
                                    "values": [
                                        {
                                            "userEnteredValue": {
                                                "stringValue": "Sample, Axa"
                                                if name == "IOL"
                                                else "Notuka Example"
                                            }
                                        }
                                    ]
                                },
                            ]
                        }
                    ],
                }
            )
        self.document: dict[str, object] = {
            "spreadsheetId": "document_synthetic",
            "sheets": sheets,
        }

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str | list[str]],
        timeout: float,
        allow_redirects: bool,
    ) -> Response:
        response = Response()
        response.status_code = 200
        response.encoding = "utf-8"
        response._content = json.dumps(self.document).encode("utf-8")
        return response

    def close(self) -> None:
        pass


def test_sheets_feeds_catalog_and_matches_with_all_the_rows(tmp_path: Path) -> None:
    session = FakeTransport()
    catalogs = JsonCatalogStore(tmp_path / "catalog.json")
    with SheetsClientReader("document_synthetic", session=session) as reader:
        service = CatalogService(
            reader_clients=reader,
            store_catalog=catalogs,
            clock=SystemClock(),
            source_id="sheets_synthetic",
        )
        catalog = service.run(dry_run=False)
        assert len(catalog.people) == 2
        assert catalogs.read() == catalog
        assert service.run() == catalog
    contacts_path = tmp_path / "week.json"
    save_json(
        contacts_path,
        {
            "period": {"start_date": "2026-11-02", "end_date": "2026-11-06"},
            "contacts": [
                {"raw_name": "Axa Sample (IOL/Balanz)", "last_activity_date": "2026-11-05"}
            ],
        },
    )
    report = JsonResolutionStore(tmp_path / "report.json")
    matching = MatchingService(
        reader_contacts=JsonContactReader(contacts_path),
        store_catalog=catalogs,
        store_aliases=JsonAliasStore(tmp_path / "aliases.json"),
        list_exclusion=JsonExclusionList(tmp_path / "exclusions.json"),
        store_resolutions=report,
        comparator=RapidFuzzComparator(),
    )
    result = matching.run(dry_run=False)
    assert len(result.resolved) == 1
    assert result.resolved[0].locations == (
        ClientLocation(sheet="IOL", row=3),
        ClientLocation(sheet="IOL", row=5),
        ClientLocation(sheet="Balanz", row=3),
    )
    assert report.read() == result


def test_change_of_sheets_during_read_preserves_catalog_previous(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    session = FakeTransport()
    path = tmp_path / "catalog.json"
    with SheetsClientReader("document_synthetic", session=session) as reader:
        service = CatalogService(
            reader_clients=reader,
            store_catalog=JsonCatalogStore(path),
            clock=SystemClock(),
            source_id="sheets_synthetic",
        )
        service.run(dry_run=False)
        original = path.read_bytes()
        query = session.get
        calls = 0

        def query_with_change(
            url: str,
            *,
            params: Mapping[str, str | list[str]],
            timeout: float,
            allow_redirects: bool,
        ) -> Response:
            nonlocal calls
            calls += 1
            if calls == 2:
                sheets = cast(list[dict[str, object]], session.document["sheets"])
                sheets[1]["bandedRanges"] = [{"bandedRangeId": 123}]
            return query(url, params=params, timeout=timeout, allow_redirects=allow_redirects)

        mocker.patch.object(session, "get", side_effect=query_with_change)
        with pytest.raises(ConcurrencyError):
            service.run(dry_run=False, force=True)
    assert path.read_bytes() == original
