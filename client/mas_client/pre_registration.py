"""Fail-closed resident runtime checks performed before permanent identity creation."""

from __future__ import annotations

import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar
from urllib.parse import urlsplit

from .agent_package_update import fetch_public_package_url, validate_registration_discovery
from .local_state import LocalStateStore, StateValidationError
from .operator_onboarding import validate_operator_config_proposal

T = TypeVar("T")


@dataclass(frozen=True)
class RuntimeReadinessChecks:
    """Runtime-specific evidence providers, scoped to the proposed resident."""

    future_session_can_access: Callable[[Path], bool]
    authoritative_https_reachable: Callable[[str], bool]
    client_runtime_can_execute: Callable[[], bool]
    execution_mechanism_can_access: Callable[[str, Path, dict[str, bool]], bool]


@dataclass(frozen=True)
class RuntimeReadinessResult:
    ready: bool
    state_root: Path
    missing: tuple[str, ...]


@dataclass(frozen=True)
class RegistrationResult:
    registration: Any
    key_handle: Any


class DefiniteAdmissionRejection(Exception):
    """A structured, completed registration rejection; no Agent was created."""

    def __init__(self, status: int, error: str):
        self.status = status
        self.error = error
        super().__init__(error)


_REFRESHABLE_ADMISSION_ERRORS = {
    "exhausted_public_cohort", "revoked_public_cohort", "expired_public_cohort",
}


def fetch_authoritative_registration(origin: str) -> dict[str, Any]:
    """Read current admission from the approved HTTPS package, bypassing local cache."""
    if not _is_https_origin(origin):
        raise StateValidationError("registration discovery requires approved HTTPS origin")
    url = origin + "/api/v1/agent-package"
    response = fetch_public_package_url(url, binary=False)
    if response.status != 200 or response.url != url or response.redirected or not isinstance(response.body, dict):
        raise StateValidationError("authoritative registration discovery failed")
    return validate_registration_discovery(response.body.get("registration"))


def register_pending_identity(
    *,
    key_handle: T,
    operator_config: dict[str, Any],
    authoritative_origin: str,
    fetch_registration: Callable[[str], dict[str, Any]],
    register_with_discovery: Callable[[T, dict[str, Any], dict[str, Any]], Any],
) -> Any:
    """Retry only a definite admission loss, once, with the same pending key.

    Transport failures, malformed responses and ambiguous outcomes propagate so
    the caller reconciles the original key before any further registration.
    """
    first = validate_registration_discovery(fetch_registration(authoritative_origin))
    if not first["available"]:
        raise StateValidationError("current registration mode is unavailable")
    try:
        return register_with_discovery(key_handle, operator_config, first)
    except DefiniteAdmissionRejection as exc:
        if exc.status != 403 or exc.error not in _REFRESHABLE_ADMISSION_ERRORS:
            raise
        refreshed = validate_registration_discovery(fetch_registration(authoritative_origin))
        if not refreshed["available"] or refreshed == first:
            raise
        return register_with_discovery(key_handle, operator_config, refreshed)


def _is_https_origin(origin: str) -> bool:
    parsed = urlsplit(origin)
    return bool(
        parsed.scheme == "https"
        and parsed.netloc
        and parsed.hostname
        and not (parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment)
    )


def _call(check: Callable[..., bool], *args: Any) -> bool:
    try:
        return check(*args) is True
    except Exception:
        return False


def _verify_private_read_write(root: Path) -> bool:
    """Verify only the intended root; leave no identity, key, or probe payload."""
    try:
        if root.is_symlink():
            return False
        if root.exists():
            allowed = {"keys"}
            if {item.name for item in root.iterdir()} - allowed:
                return False
            key_directory = root / "keys"
            if key_directory.exists() and (key_directory.is_symlink() or any(key_directory.iterdir())):
                return False
        store = LocalStateStore(root)
        store.initialize()
        if stat.S_IMODE(root.stat().st_mode) != 0o700:
            return False
        if stat.S_IMODE((root / "keys").stat().st_mode) != 0o700:
            return False
        name = f".resident-readiness-{secrets.token_hex(8)}"
        path = root / name
        payload = secrets.token_bytes(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            if path.read_bytes() != payload or stat.S_IMODE(path.stat().st_mode) != 0o600:
                return False
        finally:
            path.unlink(missing_ok=True)
        directory_fd = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return True
    except (OSError, StateValidationError):
        return False


def check_resident_runtime_readiness(
    *,
    state_root: Path,
    authoritative_origin: str,
    operator_config: dict[str, Any],
    checks: RuntimeReadinessChecks,
) -> RuntimeReadinessResult:
    """Collect readiness evidence without generating a key or contacting registration."""
    validate_operator_config_proposal(operator_config)
    root = Path(state_root)
    missing: list[str] = []

    if not _verify_private_read_write(root):
        missing.append("persistent private read/write access to the intended MAS resident state root")
    if not _call(checks.future_session_can_access, root):
        missing.append("future sessions/wakes access to that same resident state root")
    if not _is_https_origin(authoritative_origin) or not _call(
        checks.authoritative_https_reachable, authoritative_origin
    ):
        missing.append("HTTPS access to the authoritative MAS origin")
    if not _call(checks.client_runtime_can_execute):
        missing.append("ability to execute the required local MAS client/runtime")
    if not _call(
        checks.execution_mechanism_can_access,
        operator_config["schedule"]["mode"],
        root,
        dict(operator_config["tools"]),
    ):
        missing.append("selected future execution mechanism access to the resident state and authorized runtime capabilities")

    return RuntimeReadinessResult(ready=not missing, state_root=root, missing=tuple(missing))


def require_resident_runtime_readiness(**kwargs: Any) -> RuntimeReadinessResult:
    result = check_resident_runtime_readiness(**kwargs)
    if not result.ready:
        raise StateValidationError("resident runtime is not ready before registration: " + "; ".join(result.missing))
    return result


def register_new_resident_after_readiness(
    *,
    state_root: Path,
    authoritative_origin: str,
    operator_config: dict[str, Any],
    checks: RuntimeReadinessChecks,
    obtain_final_approval: Callable[[dict[str, Any], RuntimeReadinessResult], bool],
    generate_and_persist_permanent_key: Callable[[Path], T],
    register_once: Callable[[T, dict[str, Any]], Any] | None = None,
    fetch_registration: Callable[[str], dict[str, Any]] | None = None,
    register_with_discovery: Callable[[T, dict[str, Any], dict[str, Any]], Any] | None = None,
) -> RegistrationResult:
    """Enforce readiness -> approval -> one pending key -> authoritative admission."""
    if register_once is not None and register_with_discovery is not None:
        raise ValueError("choose one registration submission callback")
    if fetch_registration is not None and register_with_discovery is None:
        raise ValueError("registration discovery requires a submit callback")
    if register_with_discovery is not None and fetch_registration is None:
        fetch_registration = fetch_authoritative_registration
    if fetch_registration is None and register_once is None:
        raise ValueError("a registration callback is required")
    readiness = require_resident_runtime_readiness(
        state_root=state_root,
        authoritative_origin=authoritative_origin,
        operator_config=operator_config,
        checks=checks,
    )
    if obtain_final_approval(operator_config, readiness) is not True:
        raise StateValidationError("complete Operator configuration was not explicitly approved")
    key_handle = generate_and_persist_permanent_key(readiness.state_root)
    if fetch_registration is not None and register_with_discovery is not None:
        registration = register_pending_identity(
            key_handle=key_handle,
            operator_config=operator_config,
            authoritative_origin=authoritative_origin,
            fetch_registration=fetch_registration,
            register_with_discovery=register_with_discovery,
        )
    else:
        assert register_once is not None
        registration = register_once(key_handle, operator_config)
    return RegistrationResult(registration=registration, key_handle=key_handle)
