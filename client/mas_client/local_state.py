"""Versioned local MAS state; no registration, scheduler, or secret handling."""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar
from urllib.parse import urlsplit


class StateValidationError(ValueError):
    """A document is malformed, unsupported, or contains secret material."""


class IdentityMissingError(FileNotFoundError):
    """The registered identity cannot be restored locally."""


class IdentityConflictError(RuntimeError):
    """A write would replace or recreate an existing Agent identity."""


class StateMissingError(FileNotFoundError):
    """A non-identity local document is absent."""


_SCHEMAS = Path(__file__).parent / "schemas"
_FILES = {"identity": "identity.json", "profile": "profile.json", "state": "state.json", "notes": "agent-notes.json"}
_SECRET_PATTERN = re.compile(r"-----BEGIN [A-Z0-9 ]*(?:PRIVATE KEY|SEED)-----")
_MAX_JSON_BYTES = 1_000_000
_PRIVATE_DOCUMENTS = {"operator-config.json", "operator-approval.json", "policy-acceptance.json", "governance-application.json", "agent-package-state.json"}
T = TypeVar("T")


def _validate_format(format_name: str, value: str, location: str) -> None:
    if format_name == "uuid":
        try:
            if str(uuid.UUID(value)) == value:
                return
        except ValueError:
            pass
        raise StateValidationError(f"{location}: expected canonical UUID")
    if format_name == "date-time":
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                return
        except ValueError:
            pass
        raise StateValidationError(f"{location}: expected timezone-aware ISO timestamp")
    if format_name == "origin":
        parsed = urlsplit(value)
        local_http = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (parsed.scheme == "https" or local_http) and parsed.netloc and parsed.hostname and not (
            parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment
        ):
            return
        raise StateValidationError(f"{location}: expected HTTPS origin or localhost HTTP origin")
    if format_name == "key-ref":
        if re.fullmatch(r"keys/[A-Za-z0-9][A-Za-z0-9._-]*\.key", value):
            return
        raise StateValidationError(f"{location}: expected relative keys/*.key reference")
    raise StateValidationError(f"{location}: unsupported schema format {format_name}")


def _validate(value: Any, schema: dict[str, Any], location: str = "$") -> None:
    if isinstance(value, str) and _SECRET_PATTERN.search(value):
        raise StateValidationError(f"{location}: private key or seed material is forbidden")
    if "const" in schema and value != schema["const"]:
        raise StateValidationError(f"{location}: incorrect constant or schema version")
    if "enum" in schema and value not in schema["enum"]:
        raise StateValidationError(f"{location}: unsupported value")
    kinds = schema.get("type")
    if kinds is not None:
        kinds = [kinds] if isinstance(kinds, str) else kinds
        checks = {
            "object": lambda: isinstance(value, dict),
            "array": lambda: isinstance(value, list),
            "string": lambda: isinstance(value, str),
            "boolean": lambda: isinstance(value, bool),
            "integer": lambda: isinstance(value, int) and not isinstance(value, bool),
            "null": lambda: value is None,
        }
        if not any(checks[kind]() for kind in kinds):
            raise StateValidationError(f"{location}: wrong JSON type")
    if isinstance(value, int) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise StateValidationError(f"{location}: below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise StateValidationError(f"{location}: above maximum")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        missing = set(schema.get("required", [])) - value.keys()
        if missing:
            raise StateValidationError(f"{location}: missing {', '.join(sorted(missing))}")
        for key, item in value.items():
            if key not in properties:
                if schema.get("additionalProperties") is False:
                    raise StateValidationError(f"{location}: unexpected field {key}")
            else:
                _validate(item, properties[key], f"{location}.{key}")
    elif isinstance(value, list):
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise StateValidationError(f"{location}: too many items")
        for index, item in enumerate(value):
            _validate(item, schema["items"], f"{location}[{index}]")
    elif isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise StateValidationError(f"{location}: too short")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise StateValidationError(f"{location}: too long")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            raise StateValidationError(f"{location}: pattern mismatch")
        if schema.get("format"):
            _validate_format(schema["format"], value, location)


def validate_document(kind: str, document: Any) -> dict[str, Any]:
    """Validate a v1 document against its bundled JSON Schema subset."""
    if kind not in _FILES:
        raise ValueError(f"unknown document kind: {kind}")
    schema = json.loads((_SCHEMAS / f"{kind}.v1.schema.json").read_text(encoding="utf-8"))
    _validate(document, schema)
    return document


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StateValidationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


class LocalStateStore:
    """Owns a private Agent root; callers keep actual key bytes separate."""

    def __init__(self, root: Path | None = None, *, expected_agent_id: str | None = None) -> None:
        self.root = Path(root) if root is not None else Path.home() / ".mas"
        self.expected_agent_id = expected_agent_id
        if expected_agent_id is not None:
            _validate_format("uuid", expected_agent_id, "expected_agent_id")

    @classmethod
    def for_agent(cls, agent_id: str, base: Path | None = None) -> "LocalStateStore":
        """Bind one Agent UUID to one private directory under the chosen base."""
        _validate_format("uuid", agent_id, "agent_id")
        base_path = Path(base) if base is not None else Path.home() / ".mas" / "agents"
        return cls(base_path / agent_id, expected_agent_id=agent_id)

    def initialize(self) -> None:
        paths = [self.root, self.root / "keys"]
        if self.expected_agent_id is not None:
            paths.insert(0, self.root.parent)
            default_parent = Path.home() / ".mas"
            if self.root.parent == default_parent / "agents":
                paths.insert(0, default_parent)
        for path in paths:
            if path.is_symlink():
                raise StateValidationError(f"refusing symlink directory: {path}")
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not path.is_dir():
                raise StateValidationError(f"not a directory: {path}")
            os.chmod(path, 0o700)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.initialize()
        fd = os.open(self.root / ".state.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _read(self, kind: str) -> dict[str, Any]:
        path = self.root / _FILES[kind]
        try:
            file_stat = path.lstat()
        except FileNotFoundError as exc:
            if kind == "identity":
                raise IdentityMissingError("identity.json is missing; recover the registered identity") from exc
            raise StateMissingError(f"{path.name} is missing") from exc
        if not stat.S_ISREG(file_stat.st_mode):
            raise StateValidationError(f"{path.name}: expected regular file, not a symlink")
        if kind == "notes" and stat.S_IMODE(file_stat.st_mode) & 0o077:
            raise StateValidationError("agent-notes.json: permissions must exclude group and other")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            payload = stream.read(_MAX_JSON_BYTES + 1)
        if len(payload) > _MAX_JSON_BYTES:
            raise StateValidationError(f"{path.name}: too large")
        try:
            data = json.loads(payload.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise StateValidationError(f"{path.name}: invalid JSON") from exc
        validate_document(kind, data)
        if kind == "identity" and self.expected_agent_id is not None and data["agent_id"] != self.expected_agent_id:
            raise IdentityConflictError("state root belongs to another Agent UUID")
        return data

    def _atomic_replace(self, kind: str, document: dict[str, Any]) -> None:
        payload = (json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        if len(payload) > _MAX_JSON_BYTES:
            raise StateValidationError(f"{kind}.json: too large")
        path = self.root / _FILES[kind]
        if path.is_symlink():
            raise StateValidationError(f"refusing symlink file: {path}")
        fd, temporary = tempfile.mkstemp(prefix=f".{kind}.", suffix=".tmp", dir=self.root)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def read_identity(self) -> dict[str, Any]:
        return self._read("identity")

    def provision_identity(self, document: dict[str, Any]) -> None:
        """Explicitly store an already server-registered identity once."""
        validate_document("identity", document)
        if self.expected_agent_id is not None and document["agent_id"] != self.expected_agent_id:
            raise IdentityConflictError("cannot provision another Agent in this state root")
        with self._locked():
            if any((self.root / filename).exists() for filename in _FILES.values()):
                raise IdentityConflictError("local state exists; recover it instead of creating another Agent")
            self._atomic_replace("identity", document)

    def update_identity(self, document: dict[str, Any]) -> None:
        """Permit config/key rotation while preserving the same Agent and origin."""
        validate_document("identity", document)
        with self._locked():
            previous = self._read("identity")
            for immutable in ("agent_id", "society_origin", "registration", "constitution_version", "constitution_sha256"):
                if previous[immutable] != document[immutable]:
                    raise IdentityConflictError(f"cannot replace identity field {immutable}")
            self._atomic_replace("identity", document)

    def read_profile(self) -> dict[str, Any]:
        self.read_identity()
        return self._read("profile")

    def write_profile(self, document: dict[str, Any]) -> None:
        validate_document("profile", document)
        with self._locked():
            self._read("identity")
            self._atomic_replace("profile", document)

    def read_state(self) -> dict[str, Any]:
        self.read_identity()
        return self._read("state")

    def write_state(self, document: dict[str, Any]) -> None:
        validate_document("state", document)
        with self._locked():
            self._read("identity")
            self._atomic_replace("state", document)

    def update_state(self, change: Callable[[dict[str, Any]], tuple[dict[str, Any], T]]) -> T:
        """Validate and commit a state change under the per-Agent file lock."""
        with self._locked():
            self._read("identity")
            current = self._read("state")
            updated, result = change(current)
            validate_document("state", updated)
            self._atomic_replace("state", updated)
            return result

    def read_private_document(self, filename: str) -> dict[str, Any]:
        if filename not in _PRIVATE_DOCUMENTS:
            raise ValueError("unsupported private document")
        path = self.root / filename
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > _MAX_JSON_BYTES:
            raise StateValidationError(f"{filename}: expected private regular file")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            raw = stream.read(_MAX_JSON_BYTES + 1)
        if len(raw) > _MAX_JSON_BYTES:
            raise StateValidationError(f"{filename}: too large")
        try:
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise StateValidationError(f"{filename}: invalid JSON") from exc
        if not isinstance(value, dict):
            raise StateValidationError(f"{filename}: expected object")
        return value

    def write_private_document(self, filename: str, value: dict[str, Any]) -> None:
        if filename not in _PRIVATE_DOCUMENTS or not isinstance(value, dict):
            raise ValueError("unsupported private document")
        payload = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        if len(payload) > _MAX_JSON_BYTES:
            raise StateValidationError(f"{filename}: too large")
        with self._locked():
            self._read("identity")
            path = self.root / filename
            if path.is_symlink():
                raise StateValidationError(f"{filename}: refusing symlink")
            fd, temporary = tempfile.mkstemp(prefix=f".{filename}.", suffix=".tmp", dir=self.root)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
