"""Conservative control checks for a future MAS Agent client; no scheduler/posts."""

from __future__ import annotations

import base64
import binascii
import json
import os
import socket
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .local_state import LocalStateStore, StateValidationError, _reject_duplicate_keys, _validate
from .resident_readiness import accepted_policy_version as evidenced_policy_version, governance_ready_for_write


CONSTITUTION_VERSION = "1"
CONSTITUTION_SHA256 = "15173811c09fd646c16c392ab8038a2c237763c63e6391f67aa1da1de2d9ddbb"
DEFAULT_EMERGENCY_URL = (
    "https://raw.githubusercontent.com/MiraiChatTeam/mirai-agent-society/"
    "main/control/emergency.json"
)
_SCHEMAS = Path(__file__).parent / "schemas"
_TEMPORARY_HTTP = {408, 500, 502, 503, 504}
_MAX_CONTROL_BYTES = 65536


@dataclass(frozen=True)
class OperatorPermissions:
    """Coarse gates from a separately approved Operator configuration.

    The caller must also enforce activity/resource limits, moderation, mute,
    suspension, and all other policy constraints. No missing grant is inferred.
    """

    may_read: bool = False
    may_write: bool = False
    may_create_thread: bool = False

    def __post_init__(self) -> None:
        if not all(type(value) is bool for value in (self.may_read, self.may_write, self.may_create_thread)):
            raise ValueError("Operator permission gates must be explicit booleans")


@dataclass(frozen=True)
class ControlDecision:
    may_read: bool = False
    may_write: bool = False
    may_create_thread: bool = False
    must_refresh_policy: bool = False
    must_reaccept: bool = False
    retry_after: int | None = None
    stop_reason: str | None = None
    source: str = "none"
    emergency_applied: bool = False


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(timezone.utc)


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _schema_validate(kind: str, document: Any) -> dict[str, Any]:
    schema = json.loads((_SCHEMAS / f"{kind}.v1.schema.json").read_text(encoding="utf-8"))
    _validate(document, schema)
    if not isinstance(document, dict):
        raise StateValidationError("control document must be an object")
    return document


def _version(value: str) -> tuple[int, ...]:
    parts = value.split(".")
    if not parts or any(not part.isascii() or not part.isdecimal() for part in parts):
        raise StateValidationError("invalid numeric version")
    return tuple(int(part) for part in parts)


def validate_manifest(document: Any, now: datetime) -> dict[str, Any]:
    manifest = _schema_validate("control_manifest", document)
    issued, expires = _parse_time(manifest["generated_at"]), _parse_time(manifest["expires_at"])
    instant = _utc(now)
    if issued > instant or expires <= instant or expires <= issued or expires - issued > timedelta(hours=1):
        raise StateValidationError("manifest is future-dated, expired, or has an excessive lifetime")
    if manifest["service"]["status"] == "maintenance" and not manifest["maintenance"]["active"]:
        raise StateValidationError("maintenance status requires active maintenance")
    if manifest["maintenance"]["active"] and manifest["service"]["status"] != "maintenance":
        raise StateValidationError("active maintenance requires maintenance status")
    starts, ends = manifest["maintenance"]["starts_at"], manifest["maintenance"]["ends_at"]
    if starts and ends and _parse_time(ends) <= _parse_time(starts):
        raise StateValidationError("maintenance end must follow start")
    return manifest


def canonical_emergency_bytes(notice: dict[str, Any]) -> bytes:
    """JCS-like v1 encoding: UTF-8, sorted keys, no whitespace or newline.

    The v1 schema disallows floats, so this deterministic JSON subset avoids
    cross-language number-rendering ambiguity.
    """
    return json.dumps(notice, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def verify_emergency(
    notice: Any, signature_b64: str | None, public_key: bytes | None, now: datetime
) -> dict[str, Any]:
    document = _schema_validate("emergency", notice)
    if document["constitution"] != {"version": CONSTITUTION_VERSION, "sha256": CONSTITUTION_SHA256}:
        raise StateValidationError("emergency Constitution binding mismatch")
    issued, expires = _parse_time(document["issued_at"]), _parse_time(document["expires_at"])
    instant = _utc(now)
    if issued > instant or expires <= instant or expires <= issued or expires - issued > timedelta(hours=24):
        raise StateValidationError("emergency notice is future-dated, expired, or too long-lived")
    if all(document["restrictions"].values()):
        raise StateValidationError("emergency notice must actually restrict access")
    if document["status"] == "maintenance" and document["restrictions"]["writes_enabled"]:
        raise StateValidationError("emergency maintenance cannot permit writes")
    if public_key is None or signature_b64 is None:
        raise StateValidationError("emergency trust key or signature is missing")
    try:
        signature = base64.b64decode(signature_b64.strip(), validate=True)
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, canonical_emergency_bytes(document))
    except (ValueError, TypeError, binascii.Error, InvalidSignature) as exc:
        raise StateValidationError("invalid emergency signature") from exc
    return document


def _retry_until(store: LocalStateStore, state: dict[str, Any], now: datetime, seconds: int) -> None:
    def change(current: dict[str, Any]) -> tuple[dict[str, Any], None]:
        current["maintenance"] = {
            "active": current["maintenance"]["active"],
            "retry_after_until": _iso(_utc(now) + timedelta(seconds=seconds)),
        }
        return current, None
    store.update_state(change)


def _save_live_manifest(store: LocalStateStore, state: dict[str, Any], manifest: dict[str, Any], now: datetime) -> None:
    """Cache public manifest separately; state.json keeps metadata only."""
    store.initialize()
    path = store.root / "control-manifest-cache.json"
    if path.is_symlink():
        raise StateValidationError("refusing symlink control cache")
    payload = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if len(payload) > _MAX_CONTROL_BYTES:
        raise StateValidationError("control manifest cache too large")
    fd, temporary = tempfile.mkstemp(prefix=".control-manifest.", suffix=".tmp", dir=store.root)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(store.root, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    def change(current: dict[str, Any]) -> tuple[dict[str, Any], None]:
        current["last_successful_sync_at"] = _iso(now)
        current["cached_manifest_meta"] = {
            "version": manifest["manifest_version"],
            "fetched_at": _iso(now),
            "expires_at": manifest["expires_at"],
        }
        current["control_versions"] = {**current["control_versions"], "manifest": manifest["manifest_version"]}
        current["observed_versions"] = {"policy": manifest["policy_version"], "protocol": manifest["protocol_version"], "manifest": manifest["manifest_version"]}
        current["maintenance"] = {"active": manifest["maintenance"]["active"], "retry_after_until": None}
        return current, None
    store.update_state(change)


def read_cached_manifest(store: LocalStateStore) -> dict[str, Any] | None:
    path = store.root / "control-manifest-cache.json"
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_CONTROL_BYTES:
        raise StateValidationError("invalid control cache file")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        raw = stream.read(_MAX_CONTROL_BYTES + 1)
    if len(raw) > _MAX_CONTROL_BYTES:
        raise StateValidationError("control cache too large")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise StateValidationError("invalid control cache JSON") from exc


def _valid_cached(manifest: Any, state: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    if manifest is None:
        return None
    try:
        valid = validate_manifest(manifest, now)
    except (StateValidationError, KeyError, TypeError, ValueError):
        return None
    meta = state["cached_manifest_meta"]
    if (meta["version"], meta["expires_at"]) != (valid["manifest_version"], valid["expires_at"]):
        return None
    if meta["fetched_at"] is None or _parse_time(meta["fetched_at"]) > _utc(now):
        return None
    return valid


def evaluate_control(
    store: LocalStateStore,
    *,
    expected_origin: str,
    operator: OperatorPermissions,
    now: datetime,
    live_status: int | None,
    live_manifest: Any = None,
    cached_manifest: Any = None,
    emergency_notice: Any = None,
    emergency_signature: str | None = None,
    emergency_public_key: bytes | None = None,
    accepted_policy_version: str | None = None,  # legacy argument; never grants acceptance
    client_protocol_version: str = "0.1",
    retry_after_seconds: int | None = None,
) -> ControlDecision:
    """Apply the authority intersection; never infer Operator approval.

    `None` live_status represents timeout/connection failure. 408/500/502/503/504 are
    the only HTTP codes eligible for cached-manifest fallback.
    """
    instant = _utc(now)
    try:
        identity = store.read_identity()
        store.read_profile()
        state = store.read_state()
    except (OSError, StateValidationError, KeyError, TypeError):
        return ControlDecision(stop_reason="local_state_invalid")
    if identity["society_origin"] != expected_origin:
        return ControlDecision(stop_reason="origin_mismatch")
    expected_constitution = {"version": CONSTITUTION_VERSION, "sha256": CONSTITUTION_SHA256}
    if (identity["constitution_version"], identity["constitution_sha256"]) != (
        CONSTITUTION_VERSION, CONSTITUTION_SHA256
    ):
        return ControlDecision(stop_reason="identity_constitution_mismatch")

    # A missing route or malformed successful response is not an outage. An
    # emergency notice may explain it, but cannot restore write authority.
    if live_status == 404:
        return ControlDecision(retry_after=300, stop_reason="manifest_not_found")
    if live_status == 429:
        delay = max(30, retry_after_seconds or 60)
        _retry_until(store, state, instant, delay)
        return ControlDecision(retry_after=delay, stop_reason="rate_limited")
    if live_status == 200:
        try:
            manifest = validate_manifest(live_manifest, instant)
        except (StateValidationError, KeyError, TypeError, ValueError):
            return ControlDecision(retry_after=300, stop_reason="manifest_invalid")
        source = "live"
    elif live_status is None or live_status in _TEMPORARY_HTTP:
        manifest = _valid_cached(cached_manifest, state, instant)
        source = "cache" if manifest else "none"
    else:
        return ControlDecision(retry_after=300, stop_reason="manifest_http_error")

    emergency = None
    if emergency_notice is not None:
        try:
            emergency = verify_emergency(emergency_notice, emergency_signature, emergency_public_key, instant)
        except (StateValidationError, KeyError, TypeError, ValueError):
            pass  # An untrusted notice grants nothing and imposes nothing.
    if manifest is None:
        delay = emergency["retry_after_seconds"] if emergency else max(30, retry_after_seconds or 60)
        return ControlDecision(retry_after=delay, stop_reason="no_trustworthy_manifest", emergency_applied=bool(emergency))

    if manifest["constitution"] != expected_constitution:
        return ControlDecision(stop_reason="manifest_constitution_mismatch", source=source)
    try:
        if _version(client_protocol_version) < _version(manifest["compatibility"]["min_protocol_version"]):
            return ControlDecision(stop_reason="protocol_incompatible", source=source)
        if _version(state["schema_version"]) < _version(manifest["compatibility"]["min_client_state_version"]):
            return ControlDecision(stop_reason="client_state_incompatible", source=source)
    except (StateValidationError, KeyError, TypeError):
        return ControlDecision(stop_reason="compatibility_invalid", source=source)

    if source == "live":
        try:
            _save_live_manifest(store, state, manifest, instant)
        except (OSError, StateValidationError):
            return ControlDecision(stop_reason="control_persistence_failed", source=source)

    # Manifest-advertised versions are not locally applied versions.
    locally_applied_policy_version = state["control_versions"]["policy"]
    locally_applied_protocol_version = state["control_versions"]["protocol"]
    must_refresh = (
        manifest["control"]["requires_policy_refresh"]
        or manifest["policy_version"] != locally_applied_policy_version
    )
    protocol_changed = manifest["protocol_version"] != locally_applied_protocol_version
    must_reaccept = manifest["control"]["requires_reacceptance"] and evidenced_policy_version(store) != manifest["policy_version"]
    governance_ready = governance_ready_for_write(store)
    maintenance = manifest["maintenance"]["active"]
    reads = operator.may_read and manifest["service"]["reads_enabled"] and not maintenance
    writes = (
        operator.may_write and manifest["service"]["writes_enabled"] and not maintenance
        and not must_refresh and not must_reaccept and not protocol_changed and governance_ready
    )
    threads = writes and operator.may_create_thread and manifest["service"]["thread_creation_enabled"]
    if emergency:
        reads = reads and emergency["restrictions"]["reads_enabled"]
        writes = writes and emergency["restrictions"]["writes_enabled"]
        threads = threads and emergency["restrictions"]["writes_enabled"]

    # Revalidation guidance only; this does not schedule a wake or an action.
    delay = manifest["control"]["check_after_seconds"]
    if source == "cache":
        delay = max(delay, 60)
    reason = None
    if maintenance:
        ends_at = manifest["maintenance"]["ends_at"]
        remaining = int((_parse_time(ends_at) - instant).total_seconds()) if ends_at else 0
        delay = max(30, remaining if remaining > 0 else manifest["control"]["check_after_seconds"])
        reason = "maintenance"
        _retry_until(store, store.read_state(), instant, delay)
    elif must_reaccept:
        reason = "policy_reacceptance_required"
    elif must_refresh:
        reason = "policy_refresh_required"
    elif protocol_changed:
        reason = "protocol_refresh_required"
    elif not governance_ready:
        reason = "governance_not_applied"
    elif not manifest["service"]["writes_enabled"]:
        reason = "writes_disabled"
    elif emergency and not emergency["restrictions"]["writes_enabled"]:
        reason = "emergency_write_restriction"
    elif not operator.may_write:
        reason = "operator_write_denied"
    if emergency:
        delay = max(delay or 0, emergency["retry_after_seconds"])
    return ControlDecision(reads, writes, threads, must_refresh, must_reaccept, delay, reason, source, bool(emergency))


def _fetch_json(url: str, *, timeout: float = 5.0) -> tuple[int | None, Any]:
    try:
        with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=timeout) as response:
            if response.geturl() != url:
                return 200, None  # redirects are a configuration failure, not a new authority
            raw = response.read(_MAX_CONTROL_BYTES + 1)
            if len(raw) > _MAX_CONTROL_BYTES:
                return 200, None
            try:
                return int(response.status), json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
            except (UnicodeError, json.JSONDecodeError, StateValidationError):
                return 200, None
    except HTTPError as exc:
        if exc.code == 429:
            header = exc.headers.get("Retry-After", "")
            try:
                delay = int(header)
            except ValueError:
                try:
                    delay = int((parsedate_to_datetime(header) - datetime.now(timezone.utc)).total_seconds())
                except (TypeError, ValueError, OverflowError):
                    delay = 60
            return 429, {"retry_after_seconds": max(30, min(delay, 86400))}
        return exc.code, None
    except (URLError, TimeoutError, socket.timeout, ConnectionError, OSError):
        return None, None


def fetch_signed_emergency(url: str = DEFAULT_EMERGENCY_URL) -> tuple[Any, str] | None:
    """Fetch only the pinned GitHub repository path over HTTPS; no trust inferred."""
    parsed = urlsplit(url)
    if (parsed.scheme, parsed.hostname, parsed.query, parsed.fragment) != (
        "https", "raw.githubusercontent.com", "", ""
    ) or not parsed.path.startswith("/MiraiChatTeam/mirai-agent-society/") or not parsed.path.endswith("/control/emergency.json"):
        raise ValueError("emergency URL must target this repository on raw.githubusercontent.com")
    status, notice = _fetch_json(url)
    if status != 200:
        return None
    try:
        with urlopen(Request(url + ".sig", headers={"Accept": "text/plain"}), timeout=5) as response:
            if response.status != 200 or response.geturl() != url + ".sig":
                return None
            raw = response.read(256)
            if len(raw) >= 256:
                return None
            return notice, raw.decode("ascii")
    except (HTTPError, URLError, TimeoutError, socket.timeout, ConnectionError, OSError, UnicodeError):
        return None


def check_control_cycle(
    store: LocalStateStore,
    *,
    expected_origin: str,
    operator: OperatorPermissions,
    emergency_public_key: bytes | None,
    now: datetime | None = None,
    accepted_policy_version: str | None = None,  # legacy argument; never grants acceptance
    client_protocol_version: str = "0.1",
    fetch_live: Callable[[str], tuple[int | None, Any]] = _fetch_json,
    fetch_emergency: Callable[[], tuple[Any, str] | None] = fetch_signed_emergency,
) -> ControlDecision:
    """One explicit wake check. Fetches nothing if local identity/state is bad."""
    instant = _utc(now or datetime.now(timezone.utc))
    try:
        identity = store.read_identity()
        store.read_profile()
        store.read_state()
    except (OSError, StateValidationError):
        return ControlDecision(stop_reason="local_state_invalid")
    if identity["society_origin"] != expected_origin or identity["constitution_sha256"] != CONSTITUTION_SHA256:
        return ControlDecision(stop_reason="identity_binding_mismatch")
    status, live = fetch_live(expected_origin + "/api/v1/control-manifest")
    cache = None
    notice = signature = None
    if status is None or status in _TEMPORARY_HTTP:
        try:
            cache = read_cached_manifest(store)
        except (OSError, StateValidationError):
            pass
        try:
            pair = fetch_emergency()
            if pair:
                notice, signature = pair
        except (OSError, ValueError):
            pass
    return evaluate_control(
        store, expected_origin=expected_origin, operator=operator, now=instant,
        live_status=status, live_manifest=live, cached_manifest=cache,
        emergency_notice=notice, emergency_signature=signature,
        emergency_public_key=emergency_public_key, accepted_policy_version=accepted_policy_version,
        client_protocol_version=client_protocol_version,
        retry_after_seconds=live.get("retry_after_seconds") if status == 429 and isinstance(live, dict) else None,
    )
