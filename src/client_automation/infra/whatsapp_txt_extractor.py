"""Explicit manifests and bounded reading of local WhatsApp text exports."""

import json
import logging
from collections.abc import Generator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from stat import S_ISREG
from typing import cast

from client_automation.core.errors import (
    ConcurrencyError,
    ConfigurationError,
    DomainValidationError,
    PersistenceError,
    StructureError,
)
from client_automation.core.models import WeeklyContact, WeeklyContacts, WeeklyPeriod
from client_automation.core.whatsapp_import import (
    ContactExtraction,
    DateOrder,
    parse_activity,
)

LOGGER = logging.getLogger(__name__)
type Object = dict[str, object]
type Fingerprint = tuple[int, int, int, int, int]


@dataclass(frozen=True, slots=True, kw_only=True, repr=False)
class _Chat:
    path: Path
    name: str
    phone: str | None
    is_group: bool
    senders: tuple[str, ...]


class WhatsAppTxtExtractor:
    """Implement ContactExtractor without WhatsApp Web or network access."""

    def __init__(
        self,
        manifest: Path,
        *,
        limit_chats: int = 500,
        limit_file_bytes: int = 25 * 1024 * 1024,
        limit_line_bytes: int = 256 * 1024,
        limit_total_bytes: int = 100 * 1024 * 1024,
    ) -> None:
        """Configure paths and limits without opening files or creating directories."""
        if any(
            type(limit) is not int or limit <= 0
            for limit in (
                limit_chats,
                limit_file_bytes,
                limit_line_bytes,
                limit_total_bytes,
            )
        ):
            raise ConfigurationError("Import limits must be positive integers.")
        self._manifest = manifest.expanduser().resolve()
        self._limit_chats = limit_chats
        self._limit_file = limit_file_bytes
        self._limit_line = limit_line_bytes
        self._limit_total = limit_total_bytes

    def extract(self, period: WeeklyPeriod) -> ContactExtraction:
        """Read and validate every TXT before returning the complete weekly input."""
        initial = _fingerprint(self._manifest)
        document = _read_manifest(self._manifest)
        chats, order, century = self._parse(document)
        fingerprints = {chat.path: _fingerprint(chat.path) for chat in chats}
        if (
            any(fingerprint[2] > self._limit_file for fingerprint in fingerprints.values())
            or sum(fingerprint[2] for fingerprint in fingerprints.values()) > self._limit_total
        ):
            raise StructureError("The exports exceed the batch size limits.")
        contacts: list[WeeklyContact] = []
        messages = 0
        for chat in chats:
            with closing(self._lines(chat.path)) as lines:
                activity = parse_activity(
                    lines,
                    period=period,
                    senders=chat.senders,
                    date_order=order,
                    short_year_century=century,
                )
            _verify_fingerprint(chat.path, fingerprints[chat.path])
            messages += activity.recognized_messages
            if activity.last_activity_date is not None:
                contacts.append(
                    WeeklyContact(
                        raw_name=chat.name,
                        phone=chat.phone,
                        is_group=chat.is_group,
                        last_activity_date=activity.last_activity_date,
                    )
                )
        _verify_fingerprint(self._manifest, initial)
        for path, fingerprint in fingerprints.items():
            _verify_fingerprint(path, fingerprint)
        result = ContactExtraction(
            week=WeeklyContacts(period=period, contacts=tuple(contacts)),
            total_chats_scanned=len(chats),
            recognized_messages=messages,
        )
        LOGGER.info(
            "TXT import validated: %s chats; %s contacts within the period.",
            len(chats),
            len(contacts),
        )
        return result

    def _parse(self, document: Object) -> tuple[tuple[_Chat, ...], DateOrder, int | None]:
        if (
            not {"schema_version", "date_order", "chats"}
            <= document.keys()
            <= {
                "schema_version",
                "date_order",
                "chats",
                "short_year_century",
            }
            or type(document["schema_version"]) is not int
            or document["schema_version"] != 1
        ):
            raise StructureError("The manifest has incompatible fields or schema version.")
        order = document["date_order"]
        century = document.get("short_year_century")
        if order not in ("day_month", "month_day") or (
            century is not None and (type(century) is not int or century not in (1900, 2000))
        ):
            raise StructureError("The manifest must declare a valid date order and century.")
        inputs = document["chats"]
        if not isinstance(inputs, list) or not 1 <= len(inputs) <= self._limit_chats:
            raise StructureError(
                "The manifest must contain a nonempty batch within the chat limit."
            )
        chats: list[_Chat] = []
        for input in cast(list[object], inputs):
            chat = _parse_chat(input, self._manifest.parent)
            if chat.path in {previous.path for previous in chats}:
                raise StructureError("The manifest repeats an exported file.")
            chats.append(chat)
        return tuple(chats), order, century

    def _lines(self, path: Path) -> Generator[str, None, None]:
        total = 0
        try:
            with path.open("rb") as file:
                while content := file.readline(min(self._limit_line, self._limit_file) + 1):
                    total += len(content)
                    if len(content) > self._limit_line or total > self._limit_file:
                        raise StructureError("An export exceeds the file or line size limit.")
                    yield content.decode("utf-8-sig")
        except UnicodeError as cause:
            raise StructureError("Exports must be UTF-8 encoded.") from cause
        except OSError as cause:
            raise PersistenceError("A WhatsApp export could not be read.") from cause


def _parse_chat(value: object, base: Path) -> _Chat:
    if not isinstance(value, dict):
        raise StructureError("Each chat must be an object in the manifest.")
    input = cast(Object, value)
    if (
        not {"file", "raw_name", "is_group", "senders"}
        <= input.keys()
        <= {
            "file",
            "raw_name",
            "is_group",
            "senders",
            "phone",
        }
    ):
        raise StructureError("A chat has missing or unknown fields.")
    file, name, group = input["file"], input["raw_name"], input["is_group"]
    phone = input.get("phone")
    senders = input["senders"]
    if (
        not isinstance(file, str)
        or not file.strip()
        or "\u0000" in file
        or not isinstance(name, str)
        or type(group) is not bool
        or not (phone is None or isinstance(phone, str))
        or not isinstance(senders, list)
        or not senders
        or any(not isinstance(sender, str) for sender in senders)
    ):
        raise StructureError("A chat has invalid field types.")
    # Validate identity before opening files; do not invent names or phone numbers.
    if (phone is not None and not phone.strip()) or (not name.strip() and phone is None):
        raise DomainValidationError("Each chat requires a nonempty name or phone number.")
    names = cast(list[str], senders)
    if len(set(names)) != len(names) or any(
        not sender or sender != sender.strip() or "\n" in sender or "\r" in sender or ": " in sender
        for sender in names
    ):
        raise DomainValidationError("Sender labels must be nonempty and unique.")
    relative = Path(file)
    if (
        relative.is_absolute()
        or relative.drive
        or any(":" in part for part in relative.parts)
        or ".." in relative.parts
        or relative.suffix.lower() != ".txt"
    ):
        raise StructureError("Chat files must be relative TXT paths without parent traversal.")
    path = (base / relative).resolve()
    if not path.is_relative_to(base) or path.suffix.lower() != ".txt":
        raise StructureError("An export resolves outside the manifest directory.")
    return _Chat(
        path=path,
        name=name,
        phone=phone,
        is_group=group,
        senders=tuple(cast(list[str], senders)),
    )


def _read_manifest(path: Path) -> Object:
    try:
        with path.open("rb") as file:
            content = file.read(1024 * 1024 + 1)
        if len(content) > 1024 * 1024:
            raise StructureError("The manifest exceeds the one MiB limit.")
        document: object = json.loads(
            content.decode("utf-8-sig"), object_pairs_hook=_without_duplicates
        )
    except (ValueError, RecursionError) as cause:
        raise StructureError("The manifest must contain valid UTF-8 JSON.") from cause
    except OSError as cause:
        raise PersistenceError("The import manifest could not be read.") from cause
    if not isinstance(document, dict):
        raise StructureError("The manifest must be a JSON object.")
    return cast(Object, document)


def _without_duplicates(pairs: list[tuple[str, object]]) -> Object:
    result: Object = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("The manifest contains duplicate keys.")
        result[key] = value
    return result


def _fingerprint(path: Path) -> Fingerprint:
    try:
        state = path.stat()
        if not S_ISREG(state.st_mode):
            raise StructureError("Import inputs must be regular files.")
        return state.st_dev, state.st_ino, state.st_size, state.st_mtime_ns, state.st_ctime_ns
    except OSError as cause:
        raise PersistenceError("An import input could not be inspected.") from cause


def _verify_fingerprint(path: Path, expected: Fingerprint) -> None:
    try:
        current = _fingerprint(path)
    except (PersistenceError, StructureError) as cause:
        raise ConcurrencyError("An input changed during import.") from cause
    if current != expected:
        raise ConcurrencyError("An input changed during import.")
