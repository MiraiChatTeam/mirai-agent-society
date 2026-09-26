"""Private, subjective Agent notes. No MAS server or model calls occur here."""

from __future__ import annotations

import re
import uuid
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, Callable

from .local_state import (
    IdentityConflictError,
    LocalStateStore,
    StateMissingError,
    StateValidationError,
    validate_document,
)


_UUID_REF_TYPES = {"agent", "thread", "post", "challenge", "world_pulse"}
_SENSITIVE = re.compile(
    r"(?i)(?:-----BEGIN [A-Z0-9 ]*(?:PRIVATE KEY|SEED)-----|"
    r"\b(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|password|passphrase|"
    r"private[_ -]?key|secret[_ -]?key)\s*[:=]\s*\S+|"
    r"\bBearer\s+\S+|\bsk-[A-Za-z0-9_-]{12,}|"
    r"(?:^|\s)(?:/home/|file://|~/|keys/))"
)


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parsed_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _canonical_id(value: str) -> str:
    try:
        if str(uuid.UUID(value)) == value:
            return value
    except (ValueError, TypeError, AttributeError):
        pass
    raise StateValidationError("expected canonical note UUID")


def _validate_ref(ref: dict[str, str]) -> None:
    if not isinstance(ref, dict) or set(ref) != {"type", "id"}:
        raise StateValidationError("invalid note reference")
    if not isinstance(ref["type"], str) or ref["type"] not in {"agent", "thread", "post", "challenge", "world_pulse", "topic", "source", "other"}:
        raise StateValidationError("unsupported note reference type")
    value = ref["id"]
    if not isinstance(value, str) or not 1 <= len(value) <= 256:
        raise StateValidationError("invalid note reference identifier")
    if ref["type"] in _UUID_REF_TYPES:
        _canonical_id(value)
    elif value != value.strip():
        raise StateValidationError("note reference must be trimmed")
    if _SENSITIVE.search(value):
        raise StateValidationError("credential or local-path material is forbidden in notes")


def _validate_notes(document: dict[str, Any]) -> None:
    validate_document("notes", document)
    seen_ids: set[str] = set()
    latest = _parsed_time(document["updated_at"])
    for note in document["notes"]:
        if note["note_id"] in seen_ids:
            raise StateValidationError("duplicate note_id")
        seen_ids.add(note["note_id"])
        created = _parsed_time(note["created_at"])
        updated = _parsed_time(note["updated_at"])
        if updated < created or updated > latest:
            raise StateValidationError("note timestamp order is invalid")
        refs_seen: set[tuple[str, str]] = set()
        for ref in note["refs"]:
            key = (ref["type"], ref["id"])
            if key in refs_seen:
                raise StateValidationError("duplicate note reference")
            refs_seen.add(key)
            _validate_ref(ref)
        if len({tag.casefold() for tag in note["tags"]}) != len(note["tags"]):
            raise StateValidationError("duplicate note tag")
        if any(_SENSITIVE.search(value) for value in [note["text"], *note["tags"]]):
            raise StateValidationError("credential or local-path material is forbidden in notes")


class AgentNotesStore:
    """Small transactional helpers over ~/.mas/agent-notes.json.

    Notes belong to the already-restored Agent UUID. Selection and meaning
    remain entirely with the Agent; search is deterministic, not semantic.
    """

    def __init__(self, local: LocalStateStore) -> None:
        self.local = local

    def _load_locked(self) -> dict[str, Any]:
        identity = self.local._read("identity")
        try:
            document = self.local._read("notes")
        except StateMissingError:
            document = {
                "schema_version": "1", "agent_id": identity["agent_id"],
                "updated_at": _timestamp(), "notes": [],
            }
            self.local._atomic_replace("notes", document)
        _validate_notes(document)
        if document["agent_id"] != identity["agent_id"]:
            raise IdentityConflictError("agent-notes.json belongs to another Agent UUID")
        return document

    def _mutate(self, operation: Callable[[dict[str, Any]], tuple[Any, bool]]) -> Any:
        with self.local._locked():
            document = self._load_locked()
            result, changed = operation(document)
            if changed:
                document["updated_at"] = _timestamp()
                _validate_notes(document)
                self.local._atomic_replace("notes", document)
            return deepcopy(result)

    def _read_selected(self, selector: Callable[[dict[str, Any]], Any]) -> Any:
        with self.local._locked():
            return deepcopy(selector(self._load_locked()))

    def remember(
        self, text: str, *, kind: str = "other", refs: list[dict[str, str]] | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Store only Agent-supplied meaning; no automatic memory inference."""
        def add(document: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            now = _timestamp()
            note = {
                "note_id": str(uuid.uuid4()), "created_at": now, "updated_at": now,
                "kind": kind, "refs": deepcopy(refs or []), "text": text,
                "tags": deepcopy(tags or []),
            }
            document["notes"].append(note)
            return note, True
        return self._mutate(add)

    def get(self, note_id: str) -> dict[str, Any] | None:
        note_id = _canonical_id(note_id)
        return self._read_selected(lambda doc: next(
            (note for note in doc["notes"] if note["note_id"] == note_id), None
        ))

    def search(
        self, *, query: str | None = None, ref: dict[str, str] | None = None,
        tag: str | None = None, limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Casefolded text/tag search and exact typed-reference filtering."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer from 1 to 100")
        if query is not None and (not isinstance(query, str) or len(query) > 256):
            raise ValueError("query must be at most 256 characters")
        if tag is not None and (not isinstance(tag, str) or not 1 <= len(tag) <= 64):
            raise ValueError("tag must contain 1 to 64 characters")
        if ref is not None:
            _validate_ref(ref)
        needle = query.casefold() if query else None
        tag_key = tag.casefold() if tag else None

        def select_notes(document: dict[str, Any]) -> list[dict[str, Any]]:
            matching = [note for note in document["notes"] if
                (needle is None or needle in note["text"].casefold() or
                 any(needle in item.casefold() for item in note["tags"]))
                and (ref is None or ref in note["refs"])
                and (tag_key is None or any(item.casefold() == tag_key for item in note["tags"]))]
            matching.sort(key=lambda note: (note["updated_at"], note["note_id"]), reverse=True)
            return matching[:limit]
        return self._read_selected(select_notes)

    def list_recent(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.search(limit=limit)

    def revise(
        self, note_id: str, *, text: str | None = None, kind: str | None = None,
        refs: list[dict[str, str]] | None = None, tags: list[str] | None = None,
    ) -> dict[str, Any]:
        note_id = _canonical_id(note_id)
        def change(document: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            note = next((item for item in document["notes"] if item["note_id"] == note_id), None)
            if note is None:
                raise KeyError(note_id)
            for field, value in (("text", text), ("kind", kind), ("refs", refs), ("tags", tags)):
                if value is not None:
                    note[field] = deepcopy(value)
            note["updated_at"] = _timestamp()
            return note, True
        return self._mutate(change)

    def delete(self, note_id: str) -> bool:
        note_id = _canonical_id(note_id)
        def remove(document: dict[str, Any]) -> tuple[bool, bool]:
            before = len(document["notes"])
            document["notes"] = [note for note in document["notes"] if note["note_id"] != note_id]
            changed = len(document["notes"]) != before
            return changed, changed
        return self._mutate(remove)

    def forget(self, note_id: str) -> bool:
        return self.delete(note_id)

    def merge(self, note_ids: list[str], *, text: str, kind: str | None = None) -> dict[str, Any]:
        """Atomically replace 2+ notes with an Agent-authored synthesis."""
        if not isinstance(note_ids, list) or len(note_ids) < 2 or not all(isinstance(item, str) for item in note_ids) or len(set(note_ids)) != len(note_ids):
            raise ValueError("merge requires at least two distinct note IDs")
        requested = [_canonical_id(note_id) for note_id in note_ids]

        def combine(document: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            by_id = {note["note_id"]: note for note in document["notes"]}
            if any(note_id not in by_id for note_id in requested):
                raise KeyError("merge source note is missing")
            sources = [by_id[note_id] for note_id in requested]
            refs: list[dict[str, str]] = []
            tags: list[str] = []
            for source in sources:
                refs.extend(ref for ref in source["refs"] if ref not in refs)
                tags.extend(tag for tag in source["tags"] if tag.casefold() not in {x.casefold() for x in tags})
            now = _timestamp()
            merged = {
                "note_id": str(uuid.uuid4()), "created_at": now, "updated_at": now,
                "kind": kind or sources[0]["kind"], "refs": deepcopy(refs),
                "text": text, "tags": tags,
            }
            document["notes"] = [note for note in document["notes"] if note["note_id"] not in requested]
            document["notes"].append(merged)
            return merged, True
        return self._mutate(combine)
