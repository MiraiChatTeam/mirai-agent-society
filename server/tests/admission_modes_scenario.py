"""Isolated admission-mode acceptance checks; never touches production data."""

import base64
import hashlib
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import func, select

from app.admin import create_invite, create_public_cohort, invite_summary, revoke_invite
from app.admission import configured_registration_policy, registration_discovery, token_hash
from app.auth import (
    create_recovery_challenge,
    get_authenticated_agent,
    register_agent,
    verify_recovery_challenge,
)
from app.db import SessionLocal
from app.main import agent_package, app
from app.models import Agent, AgentAdmission, AgentSession, RegistrationInvite
from app.schemas import (
    AgentRegistrationCreate,
    AuthVerifyCreate,
    RecoveryChallengeCreate,
)
from app.services import APIError
from tests.integration_scenario import clear_rate_limits, prime_limit
from tests.scenario_guard import require_isolated_test_environment


@contextmanager
def admission_env(mode: str, *, code: str | None = None, cohort: str = "open-test", fallback: str | None = None):
    names = (
        "MAS_REGISTRATION_MODE",
        "MAS_PUBLIC_COHORT_CODE",
        "MAS_PUBLIC_COHORT_FALLBACK",
        "MAS_OPEN_ADMISSION_COHORT",
        "RATE_LIMIT_REGISTRATION",
    )
    before = {name: os.environ.get(name) for name in names}
    os.environ["MAS_REGISTRATION_MODE"] = mode
    os.environ["RATE_LIMIT_REGISTRATION"] = "86400"
    if code is None:
        os.environ.pop("MAS_PUBLIC_COHORT_CODE", None)
    else:
        os.environ["MAS_PUBLIC_COHORT_CODE"] = code
    if fallback is None:
        os.environ.pop("MAS_PUBLIC_COHORT_FALLBACK", None)
    else:
        os.environ["MAS_PUBLIC_COHORT_FALLBACK"] = fallback
    os.environ["MAS_OPEN_ADMISSION_COHORT"] = cohort
    try:
        yield
    finally:
        for name, value in before.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def public_key_b64(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")


def http_request(ip: str) -> Request:
    return Request({
        "type": "http",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/agents",
        "raw_path": b"/api/v1/agents",
        "query_string": b"",
        "headers": [],
        "client": (ip, 1234),
        "server": ("test", 80),
    })


def package_request(ip: str) -> Request:
    request = http_request(ip)
    request.scope["app"] = app
    return request


def register(
    key: Ed25519PrivateKey,
    name: str,
    *,
    invite_token: str | None = None,
    admission_code: str | None = None,
    ip: str = "127.0.0.1",
):
    payload = AgentRegistrationCreate(
        public_key=public_key_b64(key),
        display_name=name,
        invite_token=invite_token,
        admission_code=admission_code,
    )
    with SessionLocal() as db:
        return register_agent(payload, http_request(ip), db)


def expect_api_error(error: str, call) -> None:
    try:
        call()
    except APIError as exc:
        assert exc.content.get("error") == error, exc.content
    else:
        raise AssertionError(f"expected {error}")


def create_public(code: str, uses: int, cohort: str) -> RegistrationInvite:
    with SessionLocal() as db:
        return create_public_cohort(
            db,
            code=code,
            max_uses=uses,
            expires_in=timedelta(days=7),
            admission_cohort=cohort,
            label="admission-mode-test",
        )


def test_private_mode() -> None:
    clear_rate_limits("registration")
    with SessionLocal() as db:
        invite, secret = create_invite(
            db,
            max_uses=1,
            expires_in=timedelta(days=7),
            label="private-test",
            admission_cohort="private-test",
        )
        invite_id = invite.invite_id
        summary = invite_summary(invite)
        assert summary["admission_mode"] == "private_invite"
        assert summary["public_code"] is None
        assert secret not in str(summary)
        assert secret not in str(invite.__dict__)
        assert invite.token_hash == token_hash(secret)

    with admission_env("private_invite"):
        expect_api_error(
            "private_invite_required",
            lambda: register(Ed25519PrivateKey.generate(), f"Missing {uuid.uuid4().hex}"),
        )
        expect_api_error(
            "invalid_invite",
            lambda: register(
                Ed25519PrivateKey.generate(),
                f"Invalid {uuid.uuid4().hex}",
                invite_token="x" * 32,
            ),
        )
        expect_api_error(
            "admission_mode_mismatch",
            lambda: register(
                Ed25519PrivateKey.generate(),
                f"Wrong field {uuid.uuid4().hex}",
                admission_code="public-code",
            ),
        )
        result = register(
            Ed25519PrivateKey.generate(),
            f"Private {uuid.uuid4().hex}",
            invite_token=secret,
        )

    with SessionLocal() as db:
        admission = db.get(AgentAdmission, result.agent_id)
        assert admission is not None
        assert admission.admission_mode == "private_invite"
        assert admission.admission_cohort == "private-test"
        assert admission.invite_id == invite_id


def test_public_capacity_and_concurrency() -> None:
    clear_rate_limits("registration")
    code = f"genesis-50-{uuid.uuid4().hex[:8]}"
    source = create_public(code, 50, "genesis-50")
    assert source.public_code == code
    assert source.token_hash == hashlib.sha256(code.encode()).hexdigest()
    assert invite_summary(source)["public_code"] == code
    try:
        create_public(code, 1, "duplicate")
    except ValueError:
        pass
    else:
        raise AssertionError("duplicate public code accepted")

    agent_ids = []
    with admission_env("public_cohort", code=code):
        with SessionLocal() as db:
            discovery = registration_discovery(db)
        assert discovery == {
            "mode": "public_cohort",
            "available": True,
            "cohort": "genesis-50",
            "code_required": True,
            "request_field": "admission_code",
            "public_code": code,
        }
        expect_api_error(
            "public_cohort_code_required",
            lambda: register(Ed25519PrivateKey.generate(), f"No code {uuid.uuid4().hex}"),
        )
        expect_api_error(
            "invalid_public_cohort",
            lambda: register(
                Ed25519PrivateKey.generate(),
                f"Wrong code {uuid.uuid4().hex}",
                admission_code="wrong-public-code",
            ),
        )
        expect_api_error(
            "admission_mode_mismatch",
            lambda: register(
                Ed25519PrivateKey.generate(),
                f"Private bypass {uuid.uuid4().hex}",
                invite_token="y" * 32,
            ),
        )
        for index in range(50):
            result = register(
                Ed25519PrivateKey.generate(),
                f"Genesis Fifty {uuid.uuid4().hex}",
                admission_code=code,
                ip=f"10.20.0.{index + 1}",
            )
            agent_ids.append(result.agent_id)
        expect_api_error(
            "exhausted_public_cohort",
            lambda: register(
                Ed25519PrivateKey.generate(),
                f"Fifty First {uuid.uuid4().hex}",
                admission_code=code,
                ip="10.20.1.1",
            ),
        )
        with SessionLocal() as db:
            unavailable = registration_discovery(db)
        assert unavailable["available"] is False
        assert unavailable["public_code"] is None

    with SessionLocal() as db:
        stored = db.get(RegistrationInvite, source.invite_id)
        assert stored is not None and stored.use_count == 50
        admissions = list(db.scalars(
            select(AgentAdmission).where(AgentAdmission.invite_id == source.invite_id)
        ))
        assert len(admissions) == 50
        assert {row.agent_id for row in admissions} == set(agent_ids)
        assert {row.admission_mode for row in admissions} == {"public_cohort"}
        assert {row.admission_cohort for row in admissions} == {"genesis-50"}
        assert db.scalar(select(func.count()).select_from(AgentSession).where(
            AgentSession.token_hash == token_hash(code)
        )) == 0
        try:
            get_authenticated_agent(
                HTTPAuthorizationCredentials(scheme="Bearer", credentials=code), db
            )
        except HTTPException as exc:
            assert exc.status_code == 401
        else:
            raise AssertionError("public cohort code authenticated as a session")

    final_code = f"final-slot-{uuid.uuid4().hex[:8]}"
    final_source = create_public(final_code, 1, "final-slot")
    with admission_env("public_cohort", code=final_code):
        def attempt(index: int):
            try:
                result = register(
                    Ed25519PrivateKey.generate(),
                    f"Final Slot {uuid.uuid4().hex}",
                    admission_code=final_code,
                    ip=f"10.30.0.{index + 1}",
                )
                return ("ok", result.agent_id)
            except APIError as exc:
                return (exc.content["error"], None)

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(attempt, range(2)))
    assert sorted(item[0] for item in outcomes) == [
        "exhausted_public_cohort",
        "ok",
    ]
    with SessionLocal() as db:
        assert db.get(RegistrationInvite, final_source.invite_id).use_count == 1
        assert db.scalar(select(func.count()).select_from(AgentAdmission).where(
            AgentAdmission.invite_id == final_source.invite_id
        )) == 1


def test_public_revoke_expiry_and_failed_rollback(
    existing_key: Ed25519PrivateKey, existing_name: str,
) -> None:
    clear_rate_limits("registration")
    revoked_code = f"revoked-{uuid.uuid4().hex[:8]}"
    revoked = create_public(revoked_code, 1, "revoked")
    with SessionLocal() as db:
        revoke_invite(db, revoked.invite_id)
    with admission_env("public_cohort", code=revoked_code, fallback="open"):
        with SessionLocal() as db:
            assert registration_discovery(db)["available"] is False
            assert registration_discovery(db)["mode"] == "public_cohort"
        expect_api_error(
            "revoked_public_cohort",
            lambda: register(
                Ed25519PrivateKey.generate(),
                f"Revoked {uuid.uuid4().hex}",
                admission_code=revoked_code,
            ),
        )

    expired_code = f"expired-{uuid.uuid4().hex[:8]}"
    expired = create_public(expired_code, 1, "expired")
    with SessionLocal.begin() as db:
        row = db.get(RegistrationInvite, expired.invite_id)
        assert row is not None
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with admission_env("public_cohort", code=expired_code, fallback="open"):
        with SessionLocal() as db:
            assert registration_discovery(db)["available"] is False
            assert registration_discovery(db)["mode"] == "public_cohort"
        expect_api_error(
            "expired_public_cohort",
            lambda: register(
                Ed25519PrivateKey.generate(),
                f"Expired {uuid.uuid4().hex}",
                admission_code=expired_code,
            ),
        )

    rollback_code = f"rollback-{uuid.uuid4().hex[:8]}"
    rollback = create_public(rollback_code, 3, "rollback")
    with admission_env("public_cohort", code=rollback_code):
        for key, name in (
            (existing_key, f"Different {uuid.uuid4().hex}"),
            (Ed25519PrivateKey.generate(), existing_name),
        ):
            try:
                register(key, name, admission_code=rollback_code)
            except HTTPException as exc:
                assert exc.status_code == 409
            else:
                raise AssertionError("duplicate identity constraint did not reject")
    with SessionLocal() as db:
        assert db.get(RegistrationInvite, rollback.invite_id).use_count == 0
        assert db.scalar(select(func.count()).select_from(AgentAdmission).where(
            AgentAdmission.invite_id == rollback.invite_id
        )) == 0


def test_open_mode_and_recovery() -> tuple[Ed25519PrivateKey, str]:
    clear_rate_limits("registration")
    key = Ed25519PrivateKey.generate()
    name = f"Open Agent {uuid.uuid4().hex}"
    with admission_env("open", cohort="open-2026"):
        with SessionLocal() as db:
            assert registration_discovery(db) == {
                "mode": "open",
                "available": True,
                "cohort": "open-2026",
                "code_required": False,
                "request_field": None,
                "public_code": None,
            }
        expect_api_error(
            "admission_credential_not_allowed",
            lambda: register(
                Ed25519PrivateKey.generate(),
                f"Open private bypass {uuid.uuid4().hex}",
                invite_token="z" * 32,
            ),
        )
        expect_api_error(
            "admission_credential_not_allowed",
            lambda: register(
                Ed25519PrivateKey.generate(),
                f"Open public bypass {uuid.uuid4().hex}",
                admission_code="public-code",
            ),
        )
        registered = register(key, name)
        with SessionLocal() as db:
            admissions_before = db.scalar(select(func.count()).select_from(AgentAdmission))
        for duplicate_key, duplicate_name in (
            (key, f"Open duplicate key {uuid.uuid4().hex}"),
            (Ed25519PrivateKey.generate(), name),
        ):
            try:
                register(duplicate_key, duplicate_name)
            except HTTPException as exc:
                assert exc.status_code == 409
            else:
                raise AssertionError("open duplicate identity constraint did not reject")
        with SessionLocal() as db:
            assert db.scalar(select(func.count()).select_from(AgentAdmission)) == admissions_before

        challenge_request = RecoveryChallengeCreate(public_key=public_key_b64(key))
        with SessionLocal() as db:
            challenge = create_recovery_challenge(
                challenge_request, http_request("10.40.0.1"), db
            )
        signature = base64.b64encode(
            key.sign(challenge.signed_message.encode("utf-8"))
        ).decode("ascii")
        with SessionLocal() as db:
            recovered = verify_recovery_challenge(
                AuthVerifyCreate(
                    challenge_id=challenge.challenge_id,
                    signature=signature,
                ),
                http_request("10.40.0.2"),
                db,
            )
        assert recovered.agent_id == registered.agent_id

    with SessionLocal() as db:
        admission = db.get(AgentAdmission, registered.agent_id)
        assert admission is not None
        original = (
            admission.agent_id,
            admission.invite_id,
            admission.admission_mode,
            admission.admission_cohort,
            admission.registered_at,
        )
        assert original[1:4] == (None, "open", "open-2026")

    with admission_env("private_invite"):
        assert configured_registration_policy().mode == "private_invite"
        with SessionLocal() as db:
            unchanged = db.get(AgentAdmission, registered.agent_id)
            assert unchanged is not None
            assert (
                unchanged.agent_id,
                unchanged.invite_id,
                unchanged.admission_mode,
                unchanged.admission_cohort,
                unchanged.registered_at,
            ) == original

    clear_rate_limits("registration")
    os.environ["RATE_LIMIT_REGISTRATION"] = "1"
    prime_limit("registration", "ip", "10.50.0.1", 1)
    agents_before = None
    with SessionLocal() as db:
        agents_before = db.scalar(select(func.count()).select_from(Agent))
    with admission_env("open", cohort="open-2026"):
        os.environ["RATE_LIMIT_REGISTRATION"] = "1"
        expect_api_error(
            "rate_limited",
            lambda: register(
                Ed25519PrivateKey.generate(),
                f"Rate limited {uuid.uuid4().hex}",
                ip="10.50.0.1",
            ),
        )
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Agent)) == agents_before
    clear_rate_limits("registration")
    return key, name


def test_explicit_fallback_boundary_and_concurrency() -> None:
    clear_rate_limits("registration")
    code = f"genesis-{uuid.uuid4().hex[:8]}"
    source = create_public(code, 50, "genesis-50")
    first_ids = []
    with admission_env("public_cohort", code=code, cohort="after-genesis", fallback="open"):
        for index in range(49):
            resident = register(
                Ed25519PrivateKey.generate(), f"Genesis Fallback {uuid.uuid4().hex}",
                admission_code=code, ip=f"10.60.0.{index + 1}",
            )
            first_ids.append(resident.agent_id)
        with SessionLocal() as db:
            last_slot = registration_discovery(db)
            package_before = agent_package(package_request("10.60.2.1"), Response(), db)
        assert package_before["registration"] == last_slot
        assert last_slot == {
            "mode": "public_cohort", "available": True,
            "cohort": "genesis-50", "code_required": True,
            "request_field": "admission_code", "public_code": code,
        }
        final = register(
            Ed25519PrivateKey.generate(), f"Genesis Final {uuid.uuid4().hex}",
            admission_code=code, ip="10.60.1.1",
        )
        first_ids.append(final.agent_id)
        with SessionLocal() as db:
            discovery = registration_discovery(db)
            package_response = Response()
            package_after = agent_package(package_request("10.60.2.2"), package_response, db)
        assert package_after["registration"] == discovery
        assert package_response.headers["cache-control"] == "no-store"
        assert package_before["registration"] != package_after["registration"]
        assert discovery == {
            "mode": "open", "available": True,
            "cohort": "after-genesis", "code_required": False,
            "request_field": None, "public_code": None,
        }
        pending_key = Ed25519PrivateKey.generate()
        pending_name = f"Fifty First {uuid.uuid4().hex}"
        expect_api_error(
            "exhausted_public_cohort",
            lambda: register(pending_key, pending_name, admission_code=code, ip="10.60.1.2"),
        )
        expect_api_error(
            "invalid_public_cohort",
            lambda: register(pending_key, pending_name, admission_code="wrong-code", ip="10.60.1.3"),
        )
        fifty_first = register(pending_key, pending_name, ip="10.60.1.4")
        with SessionLocal.begin() as db:
            db.get(RegistrationInvite, source.invite_id).expires_at = datetime.now(UTC) - timedelta(seconds=1)
        with SessionLocal() as db:
            assert registration_discovery(db)["mode"] == "open"  # full before expiry
        later_open = register(
            Ed25519PrivateKey.generate(), f"Later Open {uuid.uuid4().hex}", ip="10.60.1.5",
        )
        with SessionLocal() as db:
            public_rows = list(db.scalars(select(AgentAdmission).where(AgentAdmission.invite_id == source.invite_id)))
            assert len(public_rows) == 50
            assert {row.agent_id for row in public_rows} == set(first_ids)
            assert {(row.admission_mode, row.admission_cohort) for row in public_rows} == {("public_cohort", "genesis-50")}
            assert db.get(RegistrationInvite, source.invite_id).use_count == 50
            later = db.get(AgentAdmission, fifty_first.agent_id)
            assert later is not None and (later.invite_id, later.admission_mode, later.admission_cohort) == (
                None, "open", "after-genesis",
            )
            assert db.get(AgentAdmission, later_open.agent_id).admission_mode == "open"
        with SessionLocal.begin() as db:
            db.get(RegistrationInvite, source.invite_id).revoked_at = datetime.now(UTC)
        with SessionLocal() as db:
            assert registration_discovery(db)["available"] is False  # explicit revocation stops fallback

    final_code = f"race-{uuid.uuid4().hex[:8]}"
    last_source = create_public(final_code, 1, "race-final")
    with admission_env("public_cohort", code=final_code, cohort="after-race", fallback="open"):
        keys = [Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()]
        def attempt(index: int):
            try:
                registered = register(
                    keys[index], f"Race Pending {uuid.uuid4().hex}",
                    admission_code=final_code, ip=f"10.61.0.{index + 1}",
                )
                return ("ok", index, registered.agent_id)
            except APIError as exc:
                return (exc.content["error"], index, None)
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(attempt, range(2)))
        assert sorted(item[0] for item in outcomes) == ["exhausted_public_cohort", "ok"]
        loser = next(item[1] for item in outcomes if item[0] == "exhausted_public_cohort")
        with SessionLocal() as db:
            assert registration_discovery(db)["mode"] == "open"
        recovered = register(
            keys[loser], f"Race Open {uuid.uuid4().hex}", ip="10.61.0.3",
        )
        with SessionLocal() as db:
            assert db.get(RegistrationInvite, last_source.invite_id).use_count == 1
            public_rows = list(db.scalars(select(AgentAdmission).where(AgentAdmission.invite_id == last_source.invite_id)))
            assert len(public_rows) == 1
            open_row = db.get(AgentAdmission, recovered.agent_id)
            assert open_row is not None and (open_row.invite_id, open_row.admission_mode, open_row.admission_cohort) == (
                None, "open", "after-race",
            )


def test_invalid_policy_fails_closed() -> None:
    with admission_env("public_cohort", code=f"missing-{uuid.uuid4().hex[:8]}", fallback="open"):
        with SessionLocal() as db:
            discovery = registration_discovery(db)
        assert discovery["mode"] == "public_cohort" and discovery["available"] is False
    with admission_env("public_cohort", code="valid-code", fallback="invalid"):
        try:
            configured_registration_policy()
        except RuntimeError:
            pass
        else:
            raise AssertionError("invalid public-cohort fallback did not fail closed")
    with admission_env("invalid-mode"):
        try:
            configured_registration_policy()
        except RuntimeError:
            pass
        else:
            raise AssertionError("invalid registration mode did not fail closed")


def main() -> None:
    require_isolated_test_environment()
    test_private_mode()
    test_public_capacity_and_concurrency()
    test_explicit_fallback_boundary_and_concurrency()
    key, name = test_open_mode_and_recovery()
    test_public_revoke_expiry_and_failed_rollback(key, name)
    test_invalid_policy_fails_closed()
    print({"status": "ok", "admission_modes": ["private_invite", "public_cohort", "open"]})


if __name__ == "__main__":
    main()
