"""Bounded, same-origin MAS package verification for a supervised Agent runtime."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .local_state import LocalStateStore, StateValidationError

STATE_FILE = "agent-package-state.json"
REQUIRED_IDS = {"skill", "onboarding", "api", "local-state", "constitution",
                "constitution-machine", "policy", "protocol", "privacy"}
GUIDANCE_IDS = {"skill", "onboarding", "api", "local-state", "flows"}
MAX_RESOURCES = 64
MAX_RESOURCE_BYTES = 1_000_000
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
_PATH = re.compile(r"(?:skill|docs)/[A-Za-z0-9._/-]+\Z")


@dataclass(frozen=True)
class PackageResponse:
    status: int
    url: str
    body: Any
    redirected: bool = False


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def fetch_public_package_url(url: str, *, binary: bool, timeout: float = 5.0) -> PackageResponse:
    """Optional stdlib adapter: bounded HTTPS GET with redirects disabled."""
    import json
    if not url.startswith("https://"):
        raise StateValidationError("package fetch requires HTTPS")
    opener = build_opener(_NoRedirect)
    try:
        with opener.open(Request(url, headers={"Accept": "application/octet-stream" if binary else "application/json"}), timeout=timeout) as response:
            raw = response.read(MAX_RESOURCE_BYTES + 1)
            if len(raw) > MAX_RESOURCE_BYTES:
                raise StateValidationError("package response exceeds size limit")
            if binary:
                body = raw
            else:
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError) as exc:
                    raise StateValidationError("invalid package JSON") from exc
            return PackageResponse(response.status, response.geturl(), body)
    except HTTPError as exc:
        return PackageResponse(exc.code, exc.geturl(), None, redirected=300 <= exc.code < 400)


class PackageTransport(Protocol):
    def fetch_package(self, url: str) -> PackageResponse: ...
    def fetch_resource(self, url: str) -> PackageResponse: ...
    def reload_guidance(self, documents: dict[str, bytes]) -> None: ...


def _now(now: datetime) -> str:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _checked_response(response: PackageResponse, expected_url: str, *, binary: bool) -> Any:
    if not isinstance(response, PackageResponse) or response.status != 200 or response.url != expected_url or response.redirected:
        raise StateValidationError("package response failed same-origin or redirect validation")
    if binary:
        if not isinstance(response.body, bytes) or len(response.body) > MAX_RESOURCE_BYTES:
            raise StateValidationError("invalid package resource payload")
    elif not isinstance(response.body, dict):
        raise StateValidationError("malformed agent-package manifest")
    return response.body


def _manifest(manifest: dict[str, Any], identity: dict[str, Any], now: datetime) -> tuple[list[dict[str, Any]], str]:
    import json
    if set(manifest) != {"package_version", "generated_at", "constitution", "required_documents", "emergency_fallback"}:
        raise StateValidationError("malformed agent-package manifest")
    if (manifest["package_version"] != "1" or not isinstance(manifest["required_documents"], list)
            or not isinstance(manifest["emergency_fallback"], str)):
        raise StateValidationError("unsupported agent-package manifest")
    try:
        generated = datetime.fromisoformat(manifest["generated_at"].replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise StateValidationError("invalid package generation time") from exc
    if generated.tzinfo is None or generated.utcoffset() is None or generated.astimezone(UTC) > now.astimezone(UTC) + timedelta(minutes=5):
        raise StateValidationError("future or unzoned package manifest")
    if manifest["constitution"] != {"version": identity["constitution_version"],
                                    "sha256": identity["constitution_sha256"]}:
        raise StateValidationError("package Constitution binding mismatch")
    entries = manifest["required_documents"]
    if not 1 <= len(entries) <= MAX_RESOURCES:
        raise StateValidationError("invalid package resource count")
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for item in entries:
        if not isinstance(item, dict) or set(item) != {"id", "version", "url", "sha256", "required"}:
            raise StateValidationError("malformed package resource")
        resource_id, url, digest, version = item["id"], item["url"], item["sha256"], item["version"]
        if not isinstance(resource_id, str) or not _ID.fullmatch(resource_id) or resource_id in seen_ids:
            raise StateValidationError("duplicate or invalid package resource ID")
        if not isinstance(url, str) or not url.startswith(identity["society_origin"] + "/agent-resources/"):
            raise StateValidationError("package resource has unapproved origin")
        path = url.removeprefix(identity["society_origin"] + "/agent-resources/")
        if not _PATH.fullmatch(path) or ".." in path.split("/") or path in seen_paths or url != identity["society_origin"] + "/agent-resources/" + path:
            raise StateValidationError("invalid package resource path")
        if not isinstance(digest, str) or not _HASH.fullmatch(digest):
            raise StateValidationError("invalid package SHA-256")
        if version is not None and (not isinstance(version, str) or not 1 <= len(version) <= 32):
            raise StateValidationError("invalid package resource version")
        if type(item["required"]) is not bool or (resource_id in REQUIRED_IDS and not item["required"]):
            raise StateValidationError("required package resource is missing")
        seen_ids.add(resource_id)
        seen_paths.add(path)
        normalized.append({"id": resource_id, "path": path, "version": version,
                           "sha256": digest, "required": item["required"], "url": url})
    if not REQUIRED_IDS <= seen_ids:
        raise StateValidationError("required package resource is missing")
    machine = next(item for item in normalized if item["id"] == "constitution-machine")
    human = next(item for item in normalized if item["id"] == "constitution")
    if human["version"] != identity["constitution_version"]:
        raise StateValidationError("human Constitution version mismatch")
    if machine["version"] != identity["constitution_version"] or machine["sha256"] != identity["constitution_sha256"]:
        raise StateValidationError("machine Constitution hash mismatch")
    fingerprint_data = {"package_version": manifest["package_version"],
                        "constitution": manifest["constitution"],
                        "emergency_fallback": manifest["emergency_fallback"],
                        "documents": sorted(normalized, key=lambda item: item["id"])}
    fingerprint = hashlib.sha256(json.dumps(fingerprint_data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return normalized, fingerprint


def _resource_path(store: LocalStateStore, resource_id: str, digest: str) -> Path:
    return store.root / "agent-package" / resource_id / (digest + ".resource")


def _read_verified(store: LocalStateStore, item: dict[str, Any]) -> bytes:
    path = _resource_path(store, item["id"], item["sha256"])
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > MAX_RESOURCE_BYTES:
        raise StateValidationError("cached package resource is not private and regular")
    with path.open("rb") as stream:
        raw = stream.read(MAX_RESOURCE_BYTES + 1)
    if hashlib.sha256(raw).hexdigest() != item["sha256"]:
        raise StateValidationError("cached package resource hash mismatch")
    return raw


def _save_resource(store: LocalStateStore, item: dict[str, Any], raw: bytes) -> None:
    path = _resource_path(store, item["id"], item["sha256"])
    for directory in (path.parent.parent, path.parent):
        if directory.is_symlink():
            raise StateValidationError("package cache directory is a symlink")
        directory.mkdir(mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
    if path.exists() or path.is_symlink():
        if _read_verified(store, item) == raw:
            return
        raise StateValidationError("cached package resource changed")
    fd, temporary = tempfile.mkstemp(prefix=".resource.", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_state(store: LocalStateStore) -> dict[str, Any] | None:
    try:
        state = store.read_private_document(STATE_FILE)
    except FileNotFoundError:
        return None
    identity = store.read_identity()
    if set(state) != {"schema_version", "agent_id", "manifest_fingerprint", "observed_manifest_fingerprint",
                      "observed_at", "resources", "pending_guidance", "constitution_hold"} or state["schema_version"] != "1" or state["agent_id"] != identity["agent_id"]:
        raise StateValidationError("package state belongs to another Agent or is malformed")
    if state["manifest_fingerprint"] is not None and (not isinstance(state["manifest_fingerprint"], str) or not _HASH.fullmatch(state["manifest_fingerprint"])):
        raise StateValidationError("invalid verified package fingerprint")
    if not isinstance(state["observed_manifest_fingerprint"], str) or not _HASH.fullmatch(state["observed_manifest_fingerprint"]):
        raise StateValidationError("invalid observed package fingerprint")
    if type(state["constitution_hold"]) is not bool:
        raise StateValidationError("invalid Constitution update hold")
    try:
        observed_at = datetime.fromisoformat(state["observed_at"].replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise StateValidationError("invalid package observation time") from exc
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise StateValidationError("untrusted package observation time")
    if not isinstance(state["resources"], dict) or not isinstance(state["pending_guidance"], list):
        raise StateValidationError("malformed package resource state")
    for resource_id, item in state["resources"].items():
        if not isinstance(resource_id, str) or not _ID.fullmatch(resource_id) or not isinstance(item, dict) or set(item) != {"id", "path", "version", "sha256", "last_verified_at", "required"}:
            raise StateValidationError("malformed verified resource entry")
        if item["id"] != resource_id or not isinstance(item["path"], str) or not _PATH.fullmatch(item["path"]) or not isinstance(item["sha256"], str) or not _HASH.fullmatch(item["sha256"]) or type(item["required"]) is not bool or (item["version"] is not None and (not isinstance(item["version"], str) or not 1 <= len(item["version"]) <= 32)):
            raise StateValidationError("invalid verified resource entry")
        try:
            verified_at = datetime.fromisoformat(item["last_verified_at"].replace("Z", "+00:00"))
        except (ValueError, AttributeError) as exc:
            raise StateValidationError("invalid resource verification time") from exc
        if verified_at.tzinfo is None or verified_at.utcoffset() is None:
            raise StateValidationError("untrusted resource verification time")
    if any(not isinstance(resource_id, str) for resource_id in state["pending_guidance"]):
        raise StateValidationError("invalid pending guidance state")
    if len(set(state["pending_guidance"])) != len(state["pending_guidance"]) or any(resource_id not in GUIDANCE_IDS or resource_id not in state["resources"] for resource_id in state["pending_guidance"]):
        raise StateValidationError("invalid pending guidance state")
    return state


def read_package_state(store: LocalStateStore) -> dict[str, Any] | None:
    """Inspect verified state; a corrupt file raises rather than resetting identity."""
    return _read_state(store)


def check_agent_package(store: LocalStateStore, transport: PackageTransport, *, now: datetime) -> dict[str, bytes]:
    """Fetch manifest, download only changed resources, then reload pending guidance.

    The runtime callback must not return until changed guidance governs subsequent
    decisions. A failure keeps pending guidance for retry after restart.
    """
    _now(now)
    identity = store.read_identity()
    if not identity["society_origin"].startswith("https://"):
        raise StateValidationError("package updates require approved HTTPS origin")
    old = _read_state(store)  # corrupt state never becomes a clean bootstrap
    manifest_url = identity["society_origin"] + "/api/v1/agent-package"
    manifest = _checked_response(transport.fetch_package(manifest_url), manifest_url, binary=False)
    try:
        entries, fingerprint = _manifest(manifest, identity, now)
    except StateValidationError as exc:
        if old is not None and "Constitution" in str(exc):
            held = dict(old)
            held["constitution_hold"] = True
            store.write_private_document(STATE_FILE, held)
        raise
    previous = old["resources"] if old else {}
    if old is None or old["observed_manifest_fingerprint"] != fingerprint:
        observed_state = dict(old) if old else {
            "schema_version": "1", "agent_id": identity["agent_id"],
            "manifest_fingerprint": None, "resources": {}, "pending_guidance": [],
            "constitution_hold": False,
        }
        observed_state["observed_manifest_fingerprint"] = fingerprint
        observed_state["observed_at"] = _now(now)
        store.write_private_document(STATE_FILE, observed_state)
    current: dict[str, dict[str, Any]] = {}
    changed: dict[str, bytes] = {}
    verified_at = _now(now)
    for item in entries:
        resource_id = item["id"]
        prior = previous.get(resource_id)
        same = prior is not None and all(prior.get(field) == item[field] for field in ("id", "path", "version", "sha256", "required"))
        if same:
            _read_verified(store, prior)
            current[resource_id] = prior
            continue
        raw = _checked_response(transport.fetch_resource(item["url"]), item["url"], binary=True)
        if hashlib.sha256(raw).hexdigest() != item["sha256"]:
            raise StateValidationError("downloaded package resource hash mismatch")
        _save_resource(store, item, raw)
        current[resource_id] = {key: item[key] for key in ("id", "path", "version", "sha256", "required")}
        current[resource_id]["last_verified_at"] = verified_at
        changed[resource_id] = raw
    if old and old["manifest_fingerprint"] == fingerprint and set(previous) != set(current):
        raise StateValidationError("package state and manifest fingerprint disagree")
    pending = sorted((set(old["pending_guidance"]) if old else set()) | (set(changed) & GUIDANCE_IDS))
    old_constitution = previous.get("constitution")
    new_constitution = current["constitution"]
    constitution_changed = old_constitution is not None and any(
        old_constitution.get(field) != new_constitution[field] for field in ("version", "sha256", "path")
    )
    state = {"schema_version": "1", "agent_id": identity["agent_id"],
             "manifest_fingerprint": fingerprint, "observed_manifest_fingerprint": fingerprint,
             "observed_at": _now(now), "resources": current, "pending_guidance": pending,
             "constitution_hold": bool((old and old["constitution_hold"]) or constitution_changed)}
    store.write_private_document(STATE_FILE, state)
    if pending:
        guidance = {resource_id: _read_verified(store, current[resource_id]) for resource_id in pending}
        transport.reload_guidance(guidance)
        state["pending_guidance"] = []
        store.write_private_document(STATE_FILE, state)
    return changed


def package_governance_matches(store: LocalStateStore) -> bool:
    """Observed package must still match locally applied governance hashes."""
    try:
        package = _read_state(store)
        application = store.read_private_document("governance-application.json")
        observed = store.read_state().get("observed_versions")
        if package is None or not isinstance(observed, dict):
            return False
        resources = package["resources"]
        return (
            not package["pending_guidance"] and not package["constitution_hold"]
            and package["manifest_fingerprint"] is not None
            and package["manifest_fingerprint"] == package["observed_manifest_fingerprint"]
            and resources["policy"]["version"] == observed["policy"] == application["policy_version"]
            and resources["protocol"]["version"] == observed["protocol"] == application["protocol_version"]
            and resources["policy"]["sha256"] == application["policy_sha256"]
            and resources["protocol"]["sha256"] == application["protocol_sha256"]
        )
    except (FileNotFoundError, KeyError, TypeError, StateValidationError):
        return False
