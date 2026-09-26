"""Isolated proof-of-possession recovery acceptance; never use production identities."""

import base64
import uuid
from datetime import UTC, datetime, timedelta

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import Agent, AgentKey, AuthChallenge, RegistrationInvite
from app.rate_limits import rule_values
from tests.integration_scenario import (
    clear_rate_limits, new_invite, prime_limit, public_key_b64,
    raw_request, register, request,
)
from tests.scenario_guard import require_isolated_test_environment


def verify(challenge: dict, key: Ed25519PrivateKey, *, expected: int = 200):
    signature = base64.b64encode(
        key.sign(challenge["signed_message"].encode("utf-8"))
    ).decode("ascii")
    return request(
        "POST", "/api/v1/auth/recovery/verify",
        {"challenge_id": challenge["challenge_id"], "signature": signature},
        expected=expected,
    )


def main() -> None:
    require_isolated_test_environment()
    clear_rate_limits()
    with SessionLocal() as db:
        agents_before = db.scalar(select(func.count()).select_from(Agent))
        keys_before = db.scalar(select(func.count()).select_from(AgentKey))

    invite_id, invite_token = new_invite()
    original_key = Ed25519PrivateKey.generate()
    registration = register(original_key, invite_token)
    original_agent_id = registration["agent_id"]
    original_key_id = registration["agent_key"]["agent_key_id"]

    # A raw public-key lookup exposes no identity, even for an unknown key.
    unknown_key = Ed25519PrivateKey.generate()
    unknown = request(
        "POST", "/api/v1/auth/recovery/challenge",
        {"public_key": public_key_b64(unknown_key)}, expected=201,
    )
    known = request(
        "POST", "/api/v1/auth/recovery/challenge",
        {"public_key": public_key_b64(original_key)}, expected=201,
    )
    assert set(known) == {
        "challenge_id", "nonce", "issued_at", "expires_at", "signed_message"
    }
    assert set(known) == set(unknown)
    assert "agent_id" not in str(known) and original_agent_id not in str(known)
    assert "agent_key_id" not in str(known) and original_key_id not in str(known)
    assert known["signed_message"].startswith("MAS-RECOVERY-V1\n")
    assert verify(unknown, unknown_key, expected=401)["detail"]
    assert verify(known, unknown_key, expected=401)["detail"]
    assert verify(known, original_key, expected=401)["detail"]  # consumed

    # A recovery challenge cannot authenticate through the normal auth route.
    challenge = request(
        "POST", "/api/v1/auth/recovery/challenge",
        {"public_key": public_key_b64(original_key)}, expected=201,
    )
    signed = base64.b64encode(
        original_key.sign(challenge["signed_message"].encode("utf-8"))
    ).decode("ascii")
    assert request(
        "POST", "/api/v1/auth/verify",
        {"challenge_id": challenge["challenge_id"], "signature": signed},
        expected=401,
    )["detail"]
    recovered = verify(challenge, original_key)
    assert recovered["agent_id"] == original_agent_id
    assert recovered["agent_key_id"] == original_key_id
    assert recovered["access_token"]
    assert request("GET", "/api/v1/me/posts", token=recovered["access_token"])["items"] == []
    assert verify(challenge, original_key, expected=401)["detail"]

    # Expired recovery proof cannot reveal the identity.
    expired = request(
        "POST", "/api/v1/auth/recovery/challenge",
        {"public_key": public_key_b64(original_key)}, expected=201,
    )
    with SessionLocal.begin() as db:
        row = db.get(AuthChallenge, uuid.UUID(expired["challenge_id"]))
        assert row is not None and row.purpose == "recovery"
        row.issued_at = datetime.now(UTC) - timedelta(minutes=2)
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert verify(expired, original_key, expected=401)["detail"]

    # An unconsumed ordinary auth challenge cannot be used for recovery.
    ordinary = request(
        "POST", "/api/v1/auth/challenge",
        {"agent_id": original_agent_id, "agent_key_id": original_key_id},
        expected=201,
    )
    ordinary_signature = base64.b64encode(
        original_key.sign(ordinary["signed_message"].encode("utf-8"))
    ).decode("ascii")
    assert request(
        "POST", "/api/v1/auth/recovery/verify",
        {"challenge_id": ordinary["challenge_id"],
         "signature": ordinary_signature},
        expected=401,
    )["detail"]
    normal_session = request(
        "POST", "/api/v1/auth/verify",
        {"challenge_id": ordinary["challenge_id"],
         "signature": ordinary_signature},
    )
    assert normal_session["access_token"]
    assert normal_session["agent_id"] == original_agent_id

    # Both public recovery operations have independent source-IP rate limits.
    clear_rate_limits("recovery_challenge")
    prime_limit("recovery_challenge", "ip", "127.0.0.1", rule_values("recovery_challenge")[0])
    code, body, headers = raw_request(
        "POST", "/api/v1/auth/recovery/challenge",
        {"public_key": public_key_b64(original_key)},
    )
    assert code == 429 and body["error"] == "rate_limited"
    assert int(next(value for key, value in headers.items() if key.lower() == "retry-after")) >= 1
    clear_rate_limits("recovery_verify")
    prime_limit("recovery_verify", "ip", "127.0.0.1", rule_values("recovery_verify")[0])
    code, body, headers = raw_request(
        "POST", "/api/v1/auth/recovery/verify",
        {"challenge_id": str(uuid.uuid4()), "signature": base64.b64encode(bytes(64)).decode()},
    )
    assert code == 429 and body["error"] == "rate_limited"
    assert int(next(value for key, value in headers.items() if key.lower() == "retry-after")) >= 1
    clear_rate_limits()

    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Agent)) == agents_before + 1
        assert db.scalar(select(func.count()).select_from(AgentKey)) == keys_before + 1
        invite = db.get(RegistrationInvite, uuid.UUID(invite_id))
        assert invite is not None and invite.use_count == 1
    print({"status": "ok", "same_agent_id": original_agent_id,
           "same_key_id": original_key_id, "invite_uses": 1})


if __name__ == "__main__":
    main()
