"""Local Operator authorization and reviewed governance for supervised residents.

These helpers do not fetch documents, obtain human approval, or schedule a wake.
The runtime must supply the actual approval reference and verified document bytes.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from .local_state import LocalStateStore, StateValidationError


def _digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _timestamp(value: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise StateValidationError("approval timestamp must be timezone-aware") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StateValidationError("approval timestamp must be timezone-aware")


def _reference(value: str) -> None:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 200 or value != value.strip():
        raise StateValidationError("a nonsecret approval reference is required")
    if any(marker in value.lower() for marker in ("bearer ", "private key", "password=", "secret=")):
        raise StateValidationError("approval reference must not contain credentials")


def _reject_secret_fields(value: Any) -> None:
    forbidden = {"invite_token", "private_key", "private_key_bytes", "access_token",
                 "api_key", "password", "passphrase", "secret", "seed_phrase"}
    if isinstance(value, dict):
        for key, item in value.items():
            if key in forbidden:
                raise StateValidationError("Operator configuration must not contain credentials")
            _reject_secret_fields(item)
    elif isinstance(value, list):
        for item in value:
            _reject_secret_fields(item)
    elif isinstance(value, str) and ("-----BEGIN PRIVATE KEY-----" in value or value.startswith("Bearer ")):
        raise StateValidationError("Operator configuration must not contain credentials")


def _config_digest(config: dict[str, Any]) -> str:
    """Server-assigned UUID can replace the null approved before registration."""
    approved_shape = deepcopy(config)
    approved_shape["identity"]["agent_id"] = None
    return _digest(approved_shape)


def _limits(config: dict[str, Any], identity: dict[str, Any]) -> tuple[int, int]:
    try:
        required = {"mas", "identity", "policy", "daily_limits", "activity", "tokens", "cost",
                    "model", "tools", "schedule", "privacy"}
        if set(config) != required:
            raise StateValidationError("approved Operator configuration must be complete")
        if not isinstance(config["model"]["resource_scopes"], list) or not config["model"]["resource_scopes"]:
            raise StateValidationError("approved model resource scope is missing")
        if config["model"]["mode"] not in {"fixed", "operator_managed", "budget_aware"}:
            raise StateValidationError("approved model mode is invalid")
        if any(type(config["tools"][name]) is not bool for name in ("web_search", "external_tools")):
            raise StateValidationError("approved tool permissions are invalid")
        if config["schedule"]["mode"] not in {"human_triggered", "scheduled_local", "provider_scheduled", "autonomous"}:
            raise StateValidationError("approved schedule mode is invalid")
        if type(config["privacy"]["disclose_operator_identity"]) is not bool:
            raise StateValidationError("approved privacy setting is invalid")
        if config["mas"]["config_version"] != identity["config_version"] or identity["config_version"] != "0.4":
            raise StateValidationError("Operator configuration version mismatch")
        if config["identity"]["agent_id"] not in (None, identity["agent_id"]):
            raise StateValidationError("Operator configuration belongs to another Agent")
        if config["daily_limits"] != {"window": "rolling_24h", "timezone": None}:
            raise StateValidationError("this resident path requires approved rolling_24h limits")
        checks = config["activity"]["max_checks_per_day"]
        actions = config["activity"]["max_actions_per_day"]
        if any(type(number) is not int or number < 0 for number in (checks, actions)):
            raise StateValidationError("Operator activity ceilings must be nonnegative integers")
        return checks, actions
    except (KeyError, TypeError) as exc:
        raise StateValidationError("approved Operator configuration is incomplete") from exc


def save_approved_operator_config(
    store: LocalStateStore, config: dict[str, Any], *, approved_at: str, approval_reference: str,
) -> None:
    """Keep the complete approved nonsecret v0.4 config beside this Agent's key."""
    identity = store.read_identity()
    _limits(config, identity)
    _reject_secret_fields(config)
    _timestamp(approved_at)
    _reference(approval_reference)
    approval = {
        "schema_version": "1", "agent_id": identity["agent_id"],
        "operator_config_id": identity["operator_config_id"],
        "config_sha256": _config_digest(config),
        "approved_at": approved_at, "approval_reference": approval_reference,
    }
    store.write_private_document("operator-config.json", config)
    store.write_private_document("operator-approval.json", approval)


def read_approved_operator_config(store: LocalStateStore) -> tuple[dict[str, Any], int, int]:
    identity = store.read_identity()
    try:
        config = store.read_private_document("operator-config.json")
        approval = store.read_private_document("operator-approval.json")
    except FileNotFoundError as exc:
        raise StateValidationError("approved Operator configuration or evidence is missing") from exc
    if set(approval) != {"schema_version", "agent_id", "operator_config_id", "config_sha256", "approved_at", "approval_reference"}:
        raise StateValidationError("Operator approval evidence is malformed")
    _timestamp(approval["approved_at"])
    _reference(approval["approval_reference"])
    checks, actions = _limits(config, identity)
    _reject_secret_fields(config)
    if (approval["schema_version"], approval["agent_id"], approval["operator_config_id"], approval["config_sha256"]) != (
        "1", identity["agent_id"], identity["operator_config_id"], _config_digest(config)
    ):
        raise StateValidationError("Operator approval does not bind this Agent and configuration")
    return config, checks, actions


def _verified_document(package: dict[str, Any], document_id: str, raw: bytes, origin: str, version: str) -> str:
    try:
        item = next(item for item in package["required_documents"] if item["id"] == document_id)
        url = urlsplit(item["url"])
        expected = urlsplit(origin)
        digest = hashlib.sha256(raw).hexdigest()
        if (url.scheme, url.netloc) != (expected.scheme, expected.netloc) or not url.path.startswith("/agent-resources/"):
            raise StateValidationError("governance document has an unapproved origin")
        if item["version"] != version or item["sha256"] != digest:
            raise StateValidationError("governance document version or hash mismatch")
        return digest
    except (KeyError, TypeError, StopIteration) as exc:
        raise StateValidationError("governance package is incomplete") from exc


def apply_reviewed_governance(
    store: LocalStateStore, *, package: dict[str, Any], policy_bytes: bytes,
    protocol_bytes: bytes, accepted_at: str, acceptance_reference: str,
    requires_reacceptance: bool,
) -> None:
    """Apply verified policy/protocol; record Operator acceptance only when required.

    The caller must actually review the documents, and must obtain genuine
    Operator approval before passing ``requires_reacceptance=True``.
    """
    identity = store.read_identity()
    observed = store.read_state().get("observed_versions")
    if not isinstance(observed, dict) or not observed.get("policy") or not observed.get("protocol"):
        raise StateValidationError("no trustworthy observed governance versions")
    if type(requires_reacceptance) is not bool:
        raise StateValidationError("requires_reacceptance must be explicit")
    _timestamp(accepted_at)
    _reference(acceptance_reference)
    if package.get("constitution") != {"version": identity["constitution_version"], "sha256": identity["constitution_sha256"]}:
        raise StateValidationError("package Constitution binding mismatch")
    policy_hash = _verified_document(package, "policy", policy_bytes, identity["society_origin"], observed["policy"])
    protocol_hash = _verified_document(package, "protocol", protocol_bytes, identity["society_origin"], observed["protocol"])
    from .agent_package_update import read_package_state
    package_state = read_package_state(store)
    if package_state is not None:
        resources = package_state["resources"]
        if (resources["policy"]["sha256"], resources["protocol"]["sha256"]) != (policy_hash, protocol_hash):
            raise StateValidationError("reviewed governance does not match verified package cache")
    application = {
        "schema_version": "1", "agent_id": identity["agent_id"],
        "policy_version": observed["policy"], "policy_sha256": policy_hash,
        "protocol_version": observed["protocol"], "protocol_sha256": protocol_hash,
        "applied_at": accepted_at, "review_reference": acceptance_reference,
    }
    if requires_reacceptance:
        acceptance = {
            "schema_version": "1", "agent_id": identity["agent_id"],
            "policy_version": observed["policy"], "policy_sha256": policy_hash,
            "protocol_version": observed["protocol"], "protocol_sha256": protocol_hash,
            "accepted_at": accepted_at, "acceptance_reference": acceptance_reference,
        }
        store.write_private_document("policy-acceptance.json", acceptance)
    store.write_private_document("governance-application.json", application)

    def apply(state: dict[str, Any]) -> tuple[dict[str, Any], None]:
        if state.get("observed_versions") != observed:
            raise StateValidationError("governance changed during review")
        state["control_versions"] = {**state["control_versions"], "policy": observed["policy"], "protocol": observed["protocol"]}
        return state, None
    store.update_state(apply)


def accepted_policy_version(store: LocalStateStore) -> str | None:
    """Return an evidenced acceptance, never a caller-asserted version."""
    try:
        evidence = store.read_private_document("policy-acceptance.json")
        identity = store.read_identity()
        _timestamp(evidence["accepted_at"])
        _reference(evidence["acceptance_reference"])
        if set(evidence) != {"schema_version", "agent_id", "policy_version", "policy_sha256",
                             "protocol_version", "protocol_sha256", "accepted_at", "acceptance_reference"}:
            return None
        if evidence["schema_version"] != "1" or evidence["agent_id"] != identity["agent_id"]:
            return None
        if any(not isinstance(evidence[name], str) or len(evidence[name]) != 64 or
               any(char not in "0123456789abcdef" for char in evidence[name])
               for name in ("policy_sha256", "protocol_sha256")):
            return None
        if not all(isinstance(evidence[name], str) and evidence[name] for name in ("policy_version", "protocol_version")):
            return None
        try:
            application = store.read_private_document("governance-application.json")
        except FileNotFoundError:
            return None
        if (evidence["policy_version"], evidence["policy_sha256"]) != (
            application.get("policy_version"), application.get("policy_sha256")
        ):
            return None
        return evidence["policy_version"]
    except (FileNotFoundError, KeyError, TypeError, StateValidationError):
        return None


def governance_ready_for_write(store: LocalStateStore) -> bool:
    try:
        state = store.read_state()
        observed = state.get("observed_versions")
        applied = state["control_versions"]
        application = store.read_private_document("governance-application.json")
        identity = store.read_identity()
        if set(application) != {"schema_version", "agent_id", "policy_version", "policy_sha256",
                                "protocol_version", "protocol_sha256", "applied_at", "review_reference"}:
            return False
        _timestamp(application["applied_at"])
        _reference(application["review_reference"])
        return (
            isinstance(observed, dict)
            and application["schema_version"] == "1"
            and application["agent_id"] == identity["agent_id"]
            and observed["policy"] == applied["policy"] == application["policy_version"]
            and observed["protocol"] == applied["protocol"] == application["protocol_version"]
            and observed["manifest"] == applied["manifest"]
            and all(isinstance(application[name], str) and len(application[name]) == 64 and
                    all(char in "0123456789abcdef" for char in application[name])
                    for name in ("policy_sha256", "protocol_sha256"))
        )
    except (FileNotFoundError, KeyError, TypeError, StateValidationError):
        return False
