"""Pure construction of people and locations with stable identities."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from uuid import NAMESPACE_URL, uuid5

from .errors import DomainValidationError
from .models import (
    ClientCatalog,
    ClientLocation,
    ClientOccurrence,
    ClientPerson,
    ClientRow,
    SheetInfo,
)
from .normalization import extract_holders, generate_name_keys


@dataclass(slots=True)
class _PersonBuilder:
    canonical_name: str
    holders: list[str] = field(default_factory=list)
    occurrences: dict[ClientLocation, ClientOccurrence] = field(default_factory=dict)


def build_catalog(
    *,
    source_id: str,
    revision: str,
    generated_at: datetime,
    sheets: tuple[SheetInfo, ...],
    clients_by_sheet: Mapping[str, tuple[ClientRow, ...]],
    previous: ClientCatalog | None = None,
) -> ClientCatalog:
    """Group normalized holders without changing source rows, cells, or prior IDs."""
    base = ClientCatalog(
        source_id=source_id,
        revision=revision,
        generated_at=generated_at,
        sheets=sheets,
        people=(),
    )
    if set(clients_by_sheet) != {sheet.name for sheet in sheets}:
        raise DomainValidationError("Catalog rows must correspond to all catalog sheets.")
    if previous is not None and previous.source_id != source_id:
        raise DomainValidationError("The previous catalog must belong to the same source.")
    accumulated: dict[str, _PersonBuilder] = {}
    for sheet in sheets:
        rows_seen: set[int] = set()
        for client in clients_by_sheet[sheet.name]:
            if client.row in rows_seen or client.row > sheet.last_client_row:
                raise DomainValidationError(
                    "Source rows cannot repeat or exceed the declared sheet bounds."
                )
            rows_seen.add(client.row)
            holders = extract_holders(client.raw_name)
            if not holders:
                raise DomainValidationError("A client cell does not contain usable holders.")
            occurrence = ClientOccurrence(
                location=ClientLocation(sheet=sheet.name, row=client.row),
                raw_name=client.raw_name,
            )
            for holder in holders:
                key = generate_name_keys(holder).sorted_name_key
                if not key:
                    raise DomainValidationError("A holder does not contain normalizable text.")
                person = accumulated.setdefault(key, _PersonBuilder(canonical_name=holder))
                if holder not in person.holders:
                    person.holders.append(holder)
                person.occurrences[occurrence.location] = occurrence

    previous_by_key = (
        {} if previous is None else {person.sorted_name_key: person for person in previous.people}
    )
    reserved = {person.person_id: key for key, person in previous_by_key.items()}
    space_source = uuid5(NAMESPACE_URL, "client-automation/catalog/v1/" + source_id)
    people: list[ClientPerson] = []
    for key, person_builder in sorted(accumulated.items()):
        person_id = (
            previous_by_key[key].person_id
            if key in previous_by_key
            else "p_" + uuid5(space_source, key).hex
        )
        if person_id in reserved and reserved[person_id] != key:
            raise DomainValidationError("A catalog identity is reserved for a different person.")
        reserved[person_id] = key
        people.append(
            ClientPerson(
                person_id=person_id,
                sorted_name_key=key,
                canonical_name=person_builder.canonical_name,
                holders=tuple(person_builder.holders),
                occurrences=tuple(person_builder.occurrences.values()),
            )
        )
    return ClientCatalog(
        source_id=base.source_id,
        revision=base.revision,
        generated_at=base.generated_at,
        sheets=base.sheets,
        people=tuple(people),
    )
