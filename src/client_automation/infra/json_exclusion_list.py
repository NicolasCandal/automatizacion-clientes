"""Persist exclusions as a JSON list of original names."""

import logging
from pathlib import Path

from client_automation.core.errors import (
    ConcurrencyError,
    DomainValidationError,
    PersistenceError,
    StructureError,
)
from client_automation.core.normalization import normalize
from client_automation.infra.json_storage import JsonValue, read_json, save_json

LOGGER = logging.getLogger(__name__)


class JsonExclusionList:
    """Implement ExclusionListPort with immediate atomic persistence."""

    def __init__(self, path: Path) -> None:
        """Load exclusions without modifying their file or creating directories."""
        self._path = path
        self._persisted = _load_exclusions(path)
        self._normalized = frozenset(normalize(name) for name in self._persisted or ())

    def contains(self, raw_name: str) -> bool:
        """Query normalized names in the snapshot without accessing the disk."""
        return normalize(raw_name) in self._normalized

    def __len__(self) -> int:
        """Return the persisted name count, including legacy duplicates."""
        return len(self._persisted or ())

    def add(self, raw_name: str) -> None:
        """Add an exclusion and update memory only after successful persistence."""
        normalized_name = _validate_name(raw_name)
        if _load_exclusions(self._path) != self._persisted:
            raise ConcurrencyError(
                "The exclusion list changed. Open a new session before adding exclusions."
            )
        if normalized_name in self._normalized:
            return

        names_updated = tuple(sorted((*(self._persisted or ()), raw_name)))
        document: list[JsonValue] = list(names_updated)
        save_json(self._path, document)
        self._persisted = names_updated
        self._normalized = self._normalized | {normalized_name}
        LOGGER.info("Exclusion registered and saved.")


def _load_exclusions(path: Path) -> tuple[str, ...] | None:
    try:
        document = read_json(path)
    except PersistenceError as cause:
        if isinstance(cause.__cause__, FileNotFoundError):
            return None
        raise

    if not isinstance(document, list):
        raise StructureError("The exclusion list must be a JSON list of strings.")
    names: list[str] = []
    for name in document:
        if not isinstance(name, str):
            raise StructureError("The exclusion list accepts only text names.")
        _validate_name(name)
        names.append(name)
    return tuple(names)


def _validate_name(raw_name: str) -> str:
    normalized_name = normalize(raw_name)
    if not normalized_name:
        raise DomainValidationError("The excluded name must contain meaningful text.")
    return normalized_name
