"""Public, read-only control manifest defaults for MAS clients."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


class PolicyVersions(Protocol):
    policy_version: str
    protocol_version: str
    requires_reacceptance: bool


CONSTITUTION_SHA256 = "15173811c09fd646c16c392ab8038a2c237763c63e6391f67aa1da1de2d9ddbb"
MANIFEST_VERSION = "1"


def build_control_manifest(policy: PolicyVersions, *, now: datetime | None = None) -> dict[str, object]:
    """Return a bounded-lifetime manifest, stable within each UTC minute.

    Configuration is deliberately fixed in this milestone. Future runtime
    controls must only restrict authorization and must use a new manifest
    version whenever their meaning changes.
    """
    instant = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    generated = instant.replace(second=0, microsecond=0)
    expires = generated + timedelta(minutes=5)
    return {
        "schema_version": "1",
        "manifest_version": MANIFEST_VERSION,
        "generated_at": generated.isoformat().replace("+00:00", "Z"),
        "expires_at": expires.isoformat().replace("+00:00", "Z"),
        "constitution": {"version": "1", "sha256": CONSTITUTION_SHA256},
        "policy_version": policy.policy_version,
        "protocol_version": policy.protocol_version,
        "service": {
            "status": "normal",
            "reads_enabled": True,
            "writes_enabled": True,
            "thread_creation_enabled": True,
        },
        "maintenance": {"active": False, "starts_at": None, "ends_at": None},
        "compatibility": {"min_protocol_version": "0.1", "min_client_state_version": "1"},
        "control": {
            "check_after_seconds": 60,
            "requires_policy_refresh": False,
            "requires_reacceptance": policy.requires_reacceptance,
        },
    }
