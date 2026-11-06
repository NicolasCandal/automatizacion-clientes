"""Legacy catalog view built by the current domain implementation."""

from client_automation.core.catalog import build_catalog as build_catalog_current
from client_automation.core.errors import ConcurrencyError
from client_automation.infra.legacy_formats import (
    LegacyCatalog,
    LegacyPerson,
    LegacySheet,
    catalog_for_matching,
)
from client_automation.infra.legacy_repository import LegacyRepository
from client_automation.infra.system_clock import SystemClock


def build_catalog(repo: LegacyRepository, source_id: str) -> LegacyCatalog:
    """Build from consistent reads and stable IDs without saving a legacy cache."""
    revision = repo.revision()
    sheets = repo.reader.list_sheets()
    clients = {sheet.name: repo.reader.read_clients(sheet.name) for sheet in sheets}
    headers = {sheet.name: repo.reader.last_week(sheet.name)[1] for sheet in sheets}
    if repo.revision() != revision:
        raise ConcurrencyError("The source changed while the catalog was being built.")
    catalog = build_catalog_current(
        source_id=source_id,
        revision=revision,
        generated_at=SystemClock().now(),
        sheets=sheets,
        clients_by_sheet=clients,
    )
    information: dict[str, LegacySheet] = {
        sheet.name: {
            "sheet_id": sheet.sheet_id,
            "name_column": sheet.name_column,
            "first_week_column": sheet.first_week_column,
            "last_week_column": sheet.last_week_column,
            "last_header_week": headers[sheet.name] or None,
            "client_rows": len(clients[sheet.name]),
            "last_client_row": sheet.last_client_row,
        }
        for sheet in sheets
    }
    people: list[LegacyPerson] = [
        {
            "id": person.person_id,
            "sorted_name_key": person.sorted_name_key,
            "canonical_name": person.canonical_name,
            "holders": list(person.holders),
            "occurrences": [
                {
                    "sheet": occurrence.location.sheet,
                    "row": occurrence.location.row,
                    "raw_name": occurrence.raw_name,
                }
                for occurrence in person.occurrences
            ],
        }
        for person in catalog.people
    ]
    return {
        "spreadsheet_id": catalog.source_id,
        "revision": catalog.revision,
        "generated_at": catalog.generated_at.isoformat(),
        "sheets": information,
        "people": people,
    }


def count_people_across_sheets(catalog: object) -> int:
    """Count people across sheets using validated domain locations."""
    return sum(len(person.sheets) > 1 for person in catalog_for_matching(catalog).people)
