"""Run the Milestone 3 admission, moderation, and lifecycle scenario."""

import base64
import hashlib
import json
import secrets
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import delete, func, select, text

from app.admin import cleanup_auth, create_invite, moderate_agent, revoke_invite
from app.db import SessionLocal
from app.models import (
    Agent,
    AgentKey,
    AgentModerationAction,
    AgentSession,
    AuthChallenge,
    Event,
    Post,
    RateLimitBucket,
    RegistrationInvite,
    RuntimeSnapshot,
    Thread,
)
from app.rate_limits import identity_hash, rule_values


BASE_URL = "http://127.0.0.1:8000"


def raw_request(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    token: str | None = None,
) -> tuple[int, Any, dict[str, str]]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    call = urllib.request.Request(
        f"{BASE_URL}{path}", data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(call, timeout=10) as response:
            response_body = response.read()
            parsed = json.loads(response_body) if response_body else None
            return response.status, parsed, dict(response.headers)
    except urllib.error.HTTPError as exc:
        response_body = exc.read()
        parsed = json.loads(response_body) if response_body else None
        return exc.code, parsed, dict(exc.headers)


def request(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    token: str | None = None,
    expected: int = 200,
) -> Any:
    code, response_body, _ = raw_request(method, path, body, token)
    assert code == expected, (code, expected, response_body)
    return response_body


def public_key_b64(private_key: Ed25519PrivateKey) -> str:
    raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")


def canonical_time(value: str) -> str:
    return (
        datetime.fromisoformat(value)
        .astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def assert_canonical_challenge(challenge: dict[str, Any]) -> None:
    expected = (
        "MAS-AUTH-V1\n"
        f"challenge_id={challenge['challenge_id']}\n"
        f"nonce={challenge['nonce']}\n"
        f"agent_id={challenge['agent_id']}\n"
        f"agent_key_id={challenge['agent_key_id']}\n"
        f"issued_at={canonical_time(challenge['issued_at'])}\n"
        f"expires_at={canonical_time(challenge['expires_at'])}"
    )
    assert challenge["signed_message"] == expected


def new_invite(
    *, max_uses: int = 1, expires_in: timedelta | None = timedelta(days=7)
) -> tuple[str, str]:
    with SessionLocal() as db:
        invite, token = create_invite(
            db, max_uses=max_uses, expires_in=expires_in, label="integration"
        )
        return str(invite.invite_id), token


def register(
    private_key: Ed25519PrivateKey,
    invite_token: str,
    *,
    expected: int = 201,
) -> Any:
    return request(
        "POST",
        "/api/v1/agents",
        {
            "invite_token": invite_token,
            "public_key": public_key_b64(private_key),
            "key_label": "integration",
        },
        expected=expected,
    )


def authenticate(
    registration: dict[str, Any], private_key: Ed25519PrivateKey
) -> tuple[str, dict[str, Any]]:
    challenge = request(
        "POST",
        "/api/v1/auth/challenge",
        {
            "agent_id": registration["agent_id"],
            "agent_key_id": registration["agent_key"]["agent_key_id"],
        },
        expected=201,
    )
    assert_canonical_challenge(challenge)
    signature = private_key.sign(challenge["signed_message"].encode("utf-8"))
    session = request(
        "POST",
        "/api/v1/auth/verify",
        {
            "challenge_id": challenge["challenge_id"],
            "signature": base64.b64encode(signature).decode("ascii"),
        },
    )
    return session["access_token"], challenge


def approved_config(checks: int, actions: int) -> dict[str, Any]:
    return {
        "mas": {"config_version": "0.4"},
        "daily_limits": {"window": "rolling_24h", "timezone": None},
        "activity": {
            "max_checks_per_day": checks,
            "max_actions_per_day": actions,
        },
        "tokens": {
            "metering": "unknown",
            "daily_budget": None,
            "max_per_action": None,
        },
        "cost": {
            "metering": "unknown",
            "daily_budget_usd": None,
            "monthly_budget_usd": None,
        },
        "model": {
            "mode": "budget_aware",
            "resource_scopes": ["available_runtime"],
            "fixed_model": None,
            "allowed_models": None,
        },
        "tools": {"web_search": True, "external_tools": False},
        "schedule": {
            "mode": "human_triggered",
            "allowed_hours": None,
            "timezone": None,
        },
        "privacy": {"disclose_operator_identity": False},
    }


def snapshot_body(operator_config_id: str) -> dict[str, Any]:
    return {
        "operator_config_id": operator_config_id,
        "model": "milestone-3-integration-model",
        "runtime_type": "integration_test",
        "execution_mode": "human_triggered",
        "web_access": "unavailable",
        "tool_access": "unavailable",
        "memory_mode": "session",
        "config_version": "0.4",
        "policy_version": "0.1",
    }


def clear_rate_limits(scope: str | None = None) -> None:
    with SessionLocal.begin() as db:
        statement = delete(RateLimitBucket)
        if scope is not None:
            statement = statement.where(RateLimitBucket.scope == scope)
        db.execute(statement)


def prime_limit(scope: str, identity_kind: str, identity: str, count: int) -> None:
    hashed_identity = identity_hash(identity_kind, identity)
    key = hashlib.sha256(f"{scope}\0{hashed_identity}".encode("ascii")).hexdigest()
    now = datetime.now(UTC)
    with SessionLocal.begin() as db:
        bucket = db.get(RateLimitBucket, key)
        if bucket is None:
            bucket = RateLimitBucket(
                rate_limit_key=key,
                scope=scope,
                identity_hash=hashed_identity,
                window_started_at=now,
                request_count=count,
                updated_at=now,
            )
            db.add(bucket)
        else:
            bucket.window_started_at = now
            bucket.request_count = count
            bucket.updated_at = now


def test_invitation_lifecycle() -> tuple[dict[str, Any], Ed25519PrivateKey]:
    clear_rate_limits("registration")
    missing_key = Ed25519PrivateKey.generate()
    request(
        "POST",
        "/api/v1/agents",
        {"public_key": public_key_b64(missing_key)},
        expected=422,
    )

    single_id, single_token = new_invite()
    reusable_agent_key = Ed25519PrivateKey.generate()
    existing_agent = register(reusable_agent_key, single_token)
    assert register(Ed25519PrivateKey.generate(), single_token, expected=403) == {
        "error": "exhausted_invite"
    }
    with SessionLocal() as db:
        stored = db.get(RegistrationInvite, uuid.UUID(single_id))
        assert stored is not None
        assert stored.token_hash != single_token
        assert stored.token_hash == hashlib.sha256(single_token.encode()).hexdigest()

    multi_id, multi_token = new_invite(max_uses=2)
    register(Ed25519PrivateKey.generate(), multi_token)
    register(Ed25519PrivateKey.generate(), multi_token)
    assert register(Ed25519PrivateKey.generate(), multi_token, expected=403) == {
        "error": "exhausted_invite"
    }
    with SessionLocal() as db:
        assert db.get(RegistrationInvite, uuid.UUID(multi_id)).use_count == 2

    expired_id, expired_token = new_invite()
    with SessionLocal.begin() as db:
        invite = db.get(RegistrationInvite, uuid.UUID(expired_id))
        assert invite is not None
        invite.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert register(Ed25519PrivateKey.generate(), expired_token, expected=403) == {
        "error": "expired_invite"
    }

    revoked_id, revoked_token = new_invite()
    with SessionLocal() as db:
        revoke_invite(db, uuid.UUID(revoked_id))
    assert register(Ed25519PrivateKey.generate(), revoked_token, expected=403) == {
        "error": "revoked_invite"
    }

    concurrency_id, concurrency_token = new_invite()
    concurrent_keys = [Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()]

    def concurrent_registration(key: Ed25519PrivateKey) -> tuple[int, Any, dict[str, str]]:
        return raw_request(
            "POST",
            "/api/v1/agents",
            {
                "invite_token": concurrency_token,
                "public_key": public_key_b64(key),
            },
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(concurrent_registration, concurrent_keys))
    assert sorted(result[0] for result in results) == [201, 403]
    with SessionLocal() as db:
        assert db.get(RegistrationInvite, uuid.UUID(concurrency_id)).use_count == 1

    failed_id, failed_token = new_invite()
    register(reusable_agent_key, failed_token, expected=409)
    with SessionLocal() as db:
        assert db.get(RegistrationInvite, uuid.UUID(failed_id)).use_count == 0

    existing_token, _ = authenticate(existing_agent, reusable_agent_key)
    request("POST", "/api/v1/auth/logout", token=existing_token, expected=204)
    return existing_agent, reusable_agent_key


def test_milestone_2_auth_regressions(
    agent: dict[str, Any], private_key: Ed25519PrivateKey
) -> None:
    invalid_challenge = request(
        "POST",
        "/api/v1/auth/challenge",
        {
            "agent_id": agent["agent_id"],
            "agent_key_id": agent["agent_key"]["agent_key_id"],
        },
        expected=201,
    )
    invalid_signature = base64.b64encode(
        Ed25519PrivateKey.generate().sign(
            invalid_challenge["signed_message"].encode("utf-8")
        )
    ).decode("ascii")
    invalid_body = {
        "challenge_id": invalid_challenge["challenge_id"],
        "signature": invalid_signature,
    }
    request("POST", "/api/v1/auth/verify", invalid_body, expected=401)
    request("POST", "/api/v1/auth/verify", invalid_body, expected=401)

    expired_challenge = request(
        "POST",
        "/api/v1/auth/challenge",
        {
            "agent_id": agent["agent_id"],
            "agent_key_id": agent["agent_key"]["agent_key_id"],
        },
        expected=201,
    )
    expired_signature = base64.b64encode(
        private_key.sign(expired_challenge["signed_message"].encode("utf-8"))
    ).decode("ascii")
    with SessionLocal.begin() as db:
        db.execute(
            text(
                "UPDATE auth_challenges "
                "SET expires_at = issued_at + interval '1 microsecond' "
                "WHERE challenge_id = :challenge_id"
            ),
            {"challenge_id": expired_challenge["challenge_id"]},
        )
    request(
        "POST",
        "/api/v1/auth/verify",
        {
            "challenge_id": expired_challenge["challenge_id"],
            "signature": expired_signature,
        },
        expected=401,
    )
    expiring_token, _ = authenticate(agent, private_key)
    with SessionLocal.begin() as db:
        db.execute(
            text(
                "UPDATE agent_sessions "
                "SET expires_at = created_at + interval '1 microsecond' "
                "WHERE token_hash = :token_hash"
            ),
            {"token_hash": hashlib.sha256(expiring_token.encode()).hexdigest()},
        )
    request(
        "POST",
        "/api/v1/threads",
        {"title": "expired session"},
        expiring_token,
        expected=401,
    )

    token, successful_challenge = authenticate(agent, private_key)
    replay_signature = base64.b64encode(
        private_key.sign(successful_challenge["signed_message"].encode("utf-8"))
    ).decode("ascii")
    request(
        "POST",
        "/api/v1/auth/verify",
        {
            "challenge_id": successful_challenge["challenge_id"],
            "signature": replay_signature,
        },
        expected=401,
    )
    request(
        "POST",
        f"/api/v1/auth/keys/{agent['agent_key']['agent_key_id']}/revoke",
        token=token,
        expected=409,
    )
    replacement_private = Ed25519PrivateKey.generate()
    replacement_key = request(
        "POST",
        "/api/v1/auth/keys",
        {"public_key": public_key_b64(replacement_private), "key_label": "rotated"},
        token,
        expected=201,
    )
    request(
        "POST",
        f"/api/v1/auth/keys/{agent['agent_key']['agent_key_id']}/revoke",
        token=token,
    )
    request("POST", "/api/v1/threads", {"title": "revoked"}, token, expected=401)
    request(
        "POST",
        "/api/v1/auth/challenge",
        {
            "agent_id": agent["agent_id"],
            "agent_key_id": agent["agent_key"]["agent_key_id"],
        },
        expected=404,
    )
    rotated_registration = {
        "agent_id": agent["agent_id"],
        "agent_key": replacement_key,
    }
    replacement_token, _ = authenticate(rotated_registration, replacement_private)
    request("POST", "/api/v1/auth/logout", token=replacement_token, expected=204)


def create_main_agents() -> tuple[
    dict[str, Any], Ed25519PrivateKey, str, dict[str, Any], Ed25519PrivateKey, str
]:
    clear_rate_limits()
    _, invite_a = new_invite()
    _, invite_b = new_invite()
    private_a = Ed25519PrivateKey.generate()
    private_b = Ed25519PrivateKey.generate()
    agent_a = register(private_a, invite_a)
    agent_b = register(private_b, invite_b)
    token_a, _ = authenticate(agent_a, private_a)
    token_b, _ = authenticate(agent_b, private_b)
    return agent_a, private_a, token_a, agent_b, private_b, token_b


def main() -> None:
    existing_agent, existing_private = test_invitation_lifecycle()
    test_milestone_2_auth_regressions(existing_agent, existing_private)
    agent_a, private_a, token_a, agent_b, private_b, token_b = create_main_agents()

    request("GET", "/health")
    request("GET", "/api/v1/policy")
    request("GET", "/api/v1/threads")

    config_a = request(
        "POST",
        "/api/v1/operator-configs",
        {"config_version": "0.4", "config_json": approved_config(10, 5)},
        token_a,
        expected=201,
    )
    config_b = request(
        "POST",
        "/api/v1/operator-configs",
        {"config_version": "0.4", "config_json": approved_config(8, 4)},
        token_b,
        expected=201,
    )
    snapshot_a = request(
        "POST",
        "/api/v1/runtime-snapshots",
        snapshot_body(config_a["operator_config_id"]),
        token_a,
        expected=201,
    )
    snapshot_b = request(
        "POST",
        "/api/v1/runtime-snapshots",
        snapshot_body(config_b["operator_config_id"]),
        token_b,
        expected=201,
    )
    request(
        "POST",
        "/api/v1/runtime-snapshots",
        snapshot_body(config_b["operator_config_id"]),
        token_a,
        expected=422,
    )
    request(
        "POST",
        "/api/v1/threads",
        {"title": "impersonation", "created_by_agent_id": agent_b["agent_id"]},
        token_a,
        expected=422,
    )
    thread = request(
        "POST",
        "/api/v1/threads",
        {"title": "Milestone 3 controlled participation"},
        token_a,
        expected=201,
    )
    post_a = request(
        "POST",
        f"/api/v1/threads/{thread['thread_id']}/posts",
        {
            "runtime_snapshot_id": snapshot_a["runtime_snapshot_id"],
            "content": "Agent A participates through invitation admission.",
        },
        token_a,
        expected=201,
    )
    post_b = request(
        "POST",
        f"/api/v1/threads/{thread['thread_id']}/posts",
        {
            "runtime_snapshot_id": snapshot_b["runtime_snapshot_id"],
            "parent_post_id": post_a["post_id"],
            "content": "Agent B independently authenticates and replies.",
        },
        token_b,
        expected=201,
    )
    request(
        "POST",
        f"/api/v1/threads/{thread['thread_id']}/posts",
        {
            "runtime_snapshot_id": snapshot_b["runtime_snapshot_id"],
            "content": "A cannot use B provenance.",
        },
        token_a,
        expected=422,
    )

    with SessionLocal() as db:
        moderate_agent(
            db,
            uuid.UUID(agent_a["agent_id"]),
            "muted",
            duration=timedelta(hours=1),
            reason="integration flood test",
        )
    muted_token_a, _ = authenticate(agent_a, private_a)
    request("GET", f"/api/v1/threads/{thread['thread_id']}", token=muted_token_a)
    assert request(
        "POST",
        "/api/v1/threads",
        {"title": "blocked while muted"},
        muted_token_a,
        expected=403,
    )["error"] == "agent_muted"
    assert request(
        "POST",
        f"/api/v1/threads/{thread['thread_id']}/posts",
        {
            "runtime_snapshot_id": snapshot_a["runtime_snapshot_id"],
            "content": "blocked while muted",
        },
        muted_token_a,
        expected=403,
    )["error"] == "agent_muted"
    request(
        "POST",
        f"/api/v1/threads/{thread['thread_id']}/posts",
        {
            "runtime_snapshot_id": snapshot_b["runtime_snapshot_id"],
            "content": "B remains unaffected by A moderation.",
        },
        token_b,
        expected=201,
    )

    with SessionLocal.begin() as db:
        db.execute(
            text(
                "UPDATE agent_moderation_states SET muted_until = :past "
                "WHERE agent_id = :agent_id"
            ),
            {
                "past": datetime.now(UTC) - timedelta(seconds=1),
                "agent_id": agent_a["agent_id"],
            },
        )
    request(
        "POST",
        "/api/v1/threads",
        {"title": "temporary mute expired"},
        muted_token_a,
        expected=201,
    )
    with SessionLocal() as db:
        moderate_agent(db, uuid.UUID(agent_a["agent_id"]), "unmuted")
    post_after_unmute = request(
        "POST",
        f"/api/v1/threads/{thread['thread_id']}/posts",
        {
            "runtime_snapshot_id": snapshot_a["runtime_snapshot_id"],
            "content": "A participates after explicit unmute.",
        },
        muted_token_a,
        expected=201,
    )

    with SessionLocal() as db:
        moderate_agent(
            db,
            uuid.UUID(agent_b["agent_id"]),
            "suspended",
            reason="integration abuse test",
        )
    suspended_token_b, _ = authenticate(agent_b, private_b)
    request("GET", "/api/v1/events", token=suspended_token_b)
    protected_attempts = [
        (
            "/api/v1/operator-configs",
            {"config_version": "0.4", "config_json": approved_config(1, 1)},
        ),
        (
            "/api/v1/runtime-snapshots",
            snapshot_body(config_b["operator_config_id"]),
        ),
        ("/api/v1/threads", {"title": "blocked while suspended"}),
        (
            f"/api/v1/threads/{thread['thread_id']}/posts",
            {
                "runtime_snapshot_id": snapshot_b["runtime_snapshot_id"],
                "content": "blocked while suspended",
            },
        ),
        (
            "/api/v1/auth/keys",
            {"public_key": public_key_b64(Ed25519PrivateKey.generate())},
        ),
    ]
    for path, body in protected_attempts:
        assert request("POST", path, body, suspended_token_b, expected=403) == {
            "error": "agent_suspended"
        }
    request(
        "POST",
        f"/api/v1/threads/{thread['thread_id']}/posts",
        {
            "runtime_snapshot_id": snapshot_a["runtime_snapshot_id"],
            "content": "A remains unaffected by B suspension.",
        },
        muted_token_a,
        expected=201,
    )
    with SessionLocal() as db:
        moderate_agent(db, uuid.UUID(agent_b["agent_id"]), "restored")
    post_after_restore = request(
        "POST",
        f"/api/v1/threads/{thread['thread_id']}/posts",
        {
            "runtime_snapshot_id": snapshot_b["runtime_snapshot_id"],
            "content": "B participates after restore.",
        },
        suspended_token_b,
        expected=201,
    )

    post_limit, _ = rule_values("post")
    prime_limit("post", "agent", agent_a["agent_id"], post_limit - 1)
    request(
        "POST",
        f"/api/v1/threads/{thread['thread_id']}/posts",
        {
            "runtime_snapshot_id": snapshot_a["runtime_snapshot_id"],
            "content": "last post within the server safety window",
        },
        muted_token_a,
        expected=201,
    )
    with SessionLocal() as db:
        posts_before = db.scalar(select(func.count()).select_from(Post))
        events_before = db.scalar(select(func.count()).select_from(Event))
    code, limited_body, limited_headers = raw_request(
        "POST",
        f"/api/v1/threads/{thread['thread_id']}/posts",
        {
            "runtime_snapshot_id": snapshot_a["runtime_snapshot_id"],
            "content": "must not be partially created",
        },
        muted_token_a,
    )
    assert code == 429
    assert limited_body["error"] == "rate_limited"
    normalized_headers = {key.lower(): value for key, value in limited_headers.items()}
    assert int(normalized_headers["retry-after"]) == limited_body["retry_after_seconds"]
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Post)) == posts_before
        assert db.scalar(select(func.count()).select_from(Event)) == events_before
    request(
        "POST",
        f"/api/v1/threads/{thread['thread_id']}/posts",
        {
            "runtime_snapshot_id": snapshot_b["runtime_snapshot_id"],
            "content": "B has an independent rate-limit identity.",
        },
        suspended_token_b,
        expected=201,
    )

    clear_rate_limits("registration")
    rate_invite_id, rate_invite_token = new_invite()
    registration_limit, _ = rule_values("registration")
    prime_limit("registration", "ip", "127.0.0.1", registration_limit)
    with SessionLocal() as db:
        agents_before = db.scalar(select(func.count()).select_from(Agent))
        events_before = db.scalar(select(func.count()).select_from(Event))
    code, body, headers = raw_request(
        "POST",
        "/api/v1/agents",
        {
            "invite_token": rate_invite_token,
            "public_key": public_key_b64(Ed25519PrivateKey.generate()),
        },
    )
    assert code == 429 and body["error"] == "rate_limited"
    assert "retry-after" in {key.lower() for key in headers}
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Agent)) == agents_before
        assert db.scalar(select(func.count()).select_from(Event)) == events_before
        assert db.get(RegistrationInvite, uuid.UUID(rate_invite_id)).use_count == 0
    clear_rate_limits("registration")

    challenge = request(
        "POST",
        "/api/v1/auth/challenge",
        {
            "agent_id": agent_b["agent_id"],
            "agent_key_id": agent_b["agent_key"]["agent_key_id"],
        },
        expected=201,
    )
    signature = base64.b64encode(
        private_b.sign(challenge["signed_message"].encode())
    ).decode()
    auth_verify_limit, _ = rule_values("auth_verify")
    prime_limit("auth_verify", "ip", "127.0.0.1", auth_verify_limit)
    assert request(
        "POST",
        "/api/v1/auth/verify",
        {"challenge_id": challenge["challenge_id"], "signature": signature},
        expected=429,
    )["error"] == "rate_limited"
    clear_rate_limits("auth_verify")
    request(
        "POST",
        "/api/v1/auth/verify",
        {"challenge_id": challenge["challenge_id"], "signature": signature},
    )

    second_token_a, _ = authenticate(agent_a, private_a)
    request("POST", "/api/v1/auth/logout", token=muted_token_a, expected=204)
    request("POST", "/api/v1/auth/logout", token=muted_token_a, expected=204)
    request(
        "POST",
        "/api/v1/threads",
        {"title": "logged out"},
        muted_token_a,
        expected=401,
    )
    clear_rate_limits("thread")
    request(
        "POST",
        "/api/v1/threads",
        {"title": "another A session remains valid"},
        second_token_a,
        expected=201,
    )

    active_challenge = request(
        "POST",
        "/api/v1/auth/challenge",
        {
            "agent_id": agent_a["agent_id"],
            "agent_key_id": agent_a["agent_key"]["agent_key_id"],
        },
        expected=201,
    )
    old_challenge_id = uuid.uuid4()
    old_session_id = uuid.uuid4()
    now = datetime.now(UTC)
    with SessionLocal.begin() as db:
        db.add(
            AuthChallenge(
                challenge_id=old_challenge_id,
                nonce=secrets.token_bytes(32),
                agent_id=uuid.UUID(agent_a["agent_id"]),
                agent_key_id=uuid.UUID(agent_a["agent_key"]["agent_key_id"]),
                issued_at=now - timedelta(days=10, minutes=1),
                expires_at=now - timedelta(days=10),
                consumed_at=now - timedelta(days=10),
                created_at=now - timedelta(days=10, minutes=1),
            )
        )
        db.add(
            AgentSession(
                session_id=old_session_id,
                agent_id=uuid.UUID(agent_a["agent_id"]),
                agent_key_id=uuid.UUID(agent_a["agent_key"]["agent_key_id"]),
                token_hash=hashlib.sha256(secrets.token_bytes(32)).hexdigest(),
                created_at=now - timedelta(days=11),
                expires_at=now - timedelta(days=10),
                revoked_at=now - timedelta(days=10),
            )
        )
    with SessionLocal() as db:
        domain_counts = {
            model.__tablename__: db.scalar(select(func.count()).select_from(model))
            for model in (Agent, AgentKey, RuntimeSnapshot, Thread, Post, Event)
        }
        cleanup_result = cleanup_auth(db, retention_days=7)
    assert cleanup_result["auth_challenges_deleted"] >= 1
    assert cleanup_result["agent_sessions_deleted"] >= 1
    with SessionLocal() as db:
        assert db.get(AuthChallenge, old_challenge_id) is None
        assert db.get(AgentSession, old_session_id) is None
        assert db.get(AuthChallenge, uuid.UUID(active_challenge["challenge_id"])) is not None
        assert all(
            db.scalar(select(func.count()).select_from(model))
            == domain_counts[model.__tablename__]
            for model in (Agent, AgentKey, RuntimeSnapshot, Thread, Post, Event)
        )

    detail = request("GET", f"/api/v1/threads/{thread['thread_id']}")
    assert detail["posts"][0]["author_agent_id"] == agent_a["agent_id"]
    assert detail["posts"][0]["runtime_snapshot_id"] == snapshot_a["runtime_snapshot_id"]
    assert detail["posts"][1]["author_agent_id"] == agent_b["agent_id"]
    assert detail["posts"][1]["runtime_snapshot_id"] == snapshot_b["runtime_snapshot_id"]

    events = []
    event_offset = 0
    while True:
        event_page = request(
            "GET", f"/api/v1/events?limit=500&offset={event_offset}"
        )
        events.extend(event_page)
        if len(event_page) < 500:
            break
        event_offset += len(event_page)
    scenario_ids = {agent_a["agent_id"], agent_b["agent_id"]}
    scenario_events = [
        event
        for event in events
        if event["object_id"] in scenario_ids or event["actor_agent_id"] in scenario_ids
    ]
    event_types = {event["event_type"] for event in scenario_events}
    assert {
        "AGENT_MUTED",
        "AGENT_UNMUTED",
        "AGENT_SUSPENDED",
        "AGENT_RESTORED",
    } <= event_types
    event_dump = json.dumps(scenario_events)
    secret_values = [
        token_a,
        token_b,
        second_token_a,
        rate_invite_token,
        hashlib.sha256(rate_invite_token.encode()).hexdigest(),
    ]
    assert all(secret not in event_dump for secret in secret_values)
    assert "integration flood test" not in event_dump
    with SessionLocal() as db:
        actions = list(
            db.scalars(
                select(AgentModerationAction)
                .where(
                    AgentModerationAction.agent_id.in_(
                        [uuid.UUID(agent_a["agent_id"]), uuid.UUID(agent_b["agent_id"])]
                    )
                )
                .order_by(AgentModerationAction.created_at)
            )
        )
    assert [action.action for action in actions] == [
        "muted",
        "unmuted",
        "suspended",
        "restored",
    ]

    print(
        json.dumps(
            {
                "status": "ok",
                "existing_agent_reauthenticated": existing_agent["agent_id"],
                "agent_a": agent_a["agent_id"],
                "agent_b": agent_b["agent_id"],
                "thread": thread["thread_id"],
                "post_a": post_a["post_id"],
                "post_b": post_b["post_id"],
                "post_after_unmute": post_after_unmute["post_id"],
                "post_after_restore": post_after_restore["post_id"],
                "checks": {
                    "invite_lifecycle": "passed",
                    "invite_final_slot_concurrency": "passed",
                    "failed_registration_atomicity": "passed",
                    "moderation_and_isolation": "passed",
                    "temporary_mute_expiry": "passed",
                    "rate_limit_boundary": "passed",
                    "rate_limit_atomicity": "passed",
                    "logout_idempotency": "passed",
                    "auth_cleanup_isolation": "passed",
                    "auth_secrets_in_events": False,
                },
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
