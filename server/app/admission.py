"""Explicit MAS registration policy and public discovery metadata."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import RegistrationInvite


AdmissionMode = Literal["private_invite", "public_cohort", "open"]
PublicCohortFallback = Literal["none", "open"]
_MODES = {"private_invite", "public_cohort", "open"}
_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,99}\Z")


@dataclass(frozen=True)
class RegistrationPolicy:
    mode: AdmissionMode
    public_code: str | None = None
    open_cohort: str | None = None
    public_cohort_fallback: PublicCohortFallback = "none"


def validate_admission_slug(value: str, field: str) -> str:
    if not isinstance(value, str) or not _SLUG.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase slug of at most 100 characters")
    return value


def configured_registration_policy() -> RegistrationPolicy:
    mode = os.getenv("MAS_REGISTRATION_MODE", "private_invite").strip()
    if mode not in _MODES:
        raise RuntimeError("MAS_REGISTRATION_MODE must be private_invite, public_cohort, or open")
    fallback = os.getenv("MAS_PUBLIC_COHORT_FALLBACK", "none").strip() or "none"
    if fallback not in {"none", "open"}:
        raise RuntimeError("MAS_PUBLIC_COHORT_FALLBACK must be none or open")
    if mode == "public_cohort":
        code = os.getenv("MAS_PUBLIC_COHORT_CODE", "").strip()
        try:
            validate_admission_slug(code, "MAS_PUBLIC_COHORT_CODE")
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        cohort = None
        if fallback == "open":
            cohort = os.getenv("MAS_OPEN_ADMISSION_COHORT", "open").strip()
            try:
                validate_admission_slug(cohort, "MAS_OPEN_ADMISSION_COHORT")
            except ValueError as exc:
                raise RuntimeError(str(exc)) from exc
        return RegistrationPolicy(
            "public_cohort", public_code=code, open_cohort=cohort,
            public_cohort_fallback=fallback,
        )
    if mode == "open":
        cohort = os.getenv("MAS_OPEN_ADMISSION_COHORT", "open").strip()
        try:
            validate_admission_slug(cohort, "MAS_OPEN_ADMISSION_COHORT")
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        return RegistrationPolicy("open", open_cohort=cohort)
    return RegistrationPolicy("private_invite")


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def public_cohort_record(
    db: Session, code: str, *, lock: bool = False,
) -> RegistrationInvite | None:
    statement = select(RegistrationInvite).where(
        RegistrationInvite.token_hash == token_hash(code),
        RegistrationInvite.admission_mode == "public_cohort",
    )
    if lock:
        statement = statement.with_for_update()
    return db.scalar(statement)


def invite_is_available(invite: RegistrationInvite, now: datetime | None = None) -> bool:
    instant = now or datetime.now(UTC)
    return (
        invite.revoked_at is None
        and (invite.expires_at is None or invite.expires_at > instant)
        and invite.use_count < invite.max_uses
    )


def public_cohort_is_exhausted(invite: RegistrationInvite | None) -> bool:
    """A completed cohort remains full after expiry; revocation still stops fallback."""
    return bool(
        invite is not None
        and invite.revoked_at is None
        and invite.use_count >= invite.max_uses
    )


def registration_discovery(db: Session) -> dict[str, object]:
    policy = configured_registration_policy()
    if policy.mode == "private_invite":
        return {
            "mode": "private_invite", "available": True,
            "cohort": None, "code_required": True,
            "request_field": "invite_token", "public_code": None,
        }
    if policy.mode == "open":
        return {
            "mode": "open", "available": True,
            "cohort": policy.open_cohort, "code_required": False,
            "request_field": None, "public_code": None,
        }
    assert policy.public_code is not None
    invite = public_cohort_record(db, policy.public_code)
    available = invite is not None and invite_is_available(invite)
    if policy.public_cohort_fallback == "open" and public_cohort_is_exhausted(invite):
        return {
            "mode": "open", "available": True,
            "cohort": policy.open_cohort, "code_required": False,
            "request_field": None, "public_code": None,
        }
    return {
        "mode": "public_cohort", "available": available,
        "cohort": invite.admission_cohort if available else None,
        "code_required": True, "request_field": "admission_code",
        "public_code": policy.public_code if available else None,
    }
