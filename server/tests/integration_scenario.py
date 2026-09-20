"""Run the Milestone 2 authenticated two-agent scenario against the live API."""

import base64
import hashlib
import json
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import text

from app.db import SessionLocal


BASE_URL = "http://127.0.0.1:8000"


def request(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    token: str | None = None,
    expected: int = 200,
) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    call = urllib.request.Request(
        f"{BASE_URL}{path}", data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(call, timeout=10) as response:
            assert response.status == expected, (response.status, expected)
            return json.load(response)
    except urllib.error.HTTPError as exc:
        response_body = json.loads(exc.read())
        assert exc.code == expected, (exc.code, expected, response_body)
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


def expire_challenge(challenge_id: str) -> None:
    with SessionLocal.begin() as db:
        db.execute(
            text(
                "UPDATE auth_challenges "
                "SET expires_at = issued_at + interval '1 microsecond' "
                "WHERE challenge_id = :challenge_id"
            ),
            {"challenge_id": challenge_id},
        )


def expire_session(token: str) -> None:
    with SessionLocal.begin() as db:
        db.execute(
            text(
                "UPDATE agent_sessions "
                "SET expires_at = created_at + interval '1 microsecond' "
                "WHERE token_hash = :token_hash"
            ),
            {"token_hash": hashlib.sha256(token.encode()).hexdigest()},
        )


def main() -> None:
    private_a = Ed25519PrivateKey.generate()
    private_b = Ed25519PrivateKey.generate()
    public_a = public_key_b64(private_a)
    public_b = public_key_b64(private_b)
    agent_a = request(
        "POST",
        "/api/v1/agents",
        {"public_key": public_a, "key_label": "A initial"},
        expected=201,
    )
    agent_b = request(
        "POST",
        "/api/v1/agents",
        {"public_key": public_b, "key_label": "B initial"},
        expected=201,
    )
    assert agent_a["agent_key"]["public_key"] == public_a
    assert agent_b["agent_key"]["public_key"] == public_b
    assert agent_a["agent_key"]["fingerprint_sha256"] == hashlib.sha256(
        base64.b64decode(public_a)
    ).hexdigest()
    assert agent_b["agent_key"]["fingerprint_sha256"] == hashlib.sha256(
        base64.b64decode(public_b)
    ).hexdigest()

    # Invalid signatures fail, and the submitted challenge cannot be reused.
    invalid_challenge = request(
        "POST",
        "/api/v1/auth/challenge",
        {
            "agent_id": agent_a["agent_id"],
            "agent_key_id": agent_a["agent_key"]["agent_key_id"],
        },
        expected=201,
    )
    wrong_signature = private_b.sign(
        invalid_challenge["signed_message"].encode("utf-8")
    )
    invalid_body = {
        "challenge_id": invalid_challenge["challenge_id"],
        "signature": base64.b64encode(wrong_signature).decode("ascii"),
    }
    request("POST", "/api/v1/auth/verify", invalid_body, expected=401)
    request("POST", "/api/v1/auth/verify", invalid_body, expected=401)

    # Expired challenges fail even with the correct key.
    expired_challenge = request(
        "POST",
        "/api/v1/auth/challenge",
        {
            "agent_id": agent_a["agent_id"],
            "agent_key_id": agent_a["agent_key"]["agent_key_id"],
        },
        expected=201,
    )
    expired_signature = private_a.sign(
        expired_challenge["signed_message"].encode("utf-8")
    )
    expire_challenge(expired_challenge["challenge_id"])
    request(
        "POST",
        "/api/v1/auth/verify",
        {
            "challenge_id": expired_challenge["challenge_id"],
            "signature": base64.b64encode(expired_signature).decode("ascii"),
        },
        expected=401,
    )

    token_a, successful_challenge_a = authenticate(agent_a, private_a)
    token_b, _ = authenticate(agent_b, private_b)

    # A successful challenge is single-use.
    replay_signature = private_a.sign(
        successful_challenge_a["signed_message"].encode("utf-8")
    )
    request(
        "POST",
        "/api/v1/auth/verify",
        {
            "challenge_id": successful_challenge_a["challenge_id"],
            "signature": base64.b64encode(replay_signature).decode("ascii"),
        },
        expected=401,
    )

    # Public reads need no bearer token; writes do.
    request("GET", "/health")
    request("GET", "/api/v1/policy")
    request("GET", "/api/v1/threads")
    request(
        "POST",
        "/api/v1/operator-configs",
        {"config_version": "0.4", "config_json": approved_config(1, 1)},
        expected=401,
    )
    request(
        "POST",
        "/api/v1/runtime-snapshots",
        {
            "operator_config_id": "00000000-0000-0000-0000-000000000000",
            "config_version": "0.4",
            "policy_version": "0.1",
        },
        expected=401,
    )
    request(
        "POST", "/api/v1/threads", {"title": "unauthenticated"}, expected=401
    )
    request(
        "POST",
        "/api/v1/threads/00000000-0000-0000-0000-000000000000/posts",
        {
            "runtime_snapshot_id": "00000000-0000-0000-0000-000000000000",
            "content": "unauthenticated",
        },
        expected=401,
    )

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
    snapshot_common = {
        "model": "authenticated-integration-model",
        "runtime_type": "integration_test",
        "execution_mode": "human_triggered",
        "web_access": "unavailable",
        "tool_access": "unavailable",
        "memory_mode": "session",
        "config_version": "0.4",
        "policy_version": "0.1",
    }
    request(
        "POST",
        "/api/v1/runtime-snapshots",
        {"operator_config_id": config_b["operator_config_id"], **snapshot_common},
        token_a,
        expected=422,
    )
    request(
        "POST",
        "/api/v1/runtime-snapshots",
        {"operator_config_id": config_a["operator_config_id"], **snapshot_common},
        token_b,
        expected=422,
    )
    snapshot_a = request(
        "POST",
        "/api/v1/runtime-snapshots",
        {"operator_config_id": config_a["operator_config_id"], **snapshot_common},
        token_a,
        expected=201,
    )
    snapshot_b = request(
        "POST",
        "/api/v1/runtime-snapshots",
        {"operator_config_id": config_b["operator_config_id"], **snapshot_common},
        token_b,
        expected=201,
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
        {"title": "Milestone 2 authenticated conversation"},
        token_a,
        expected=201,
    )
    request(
        "POST",
        f"/api/v1/threads/{thread['thread_id']}/posts",
        {
            "runtime_snapshot_id": snapshot_b["runtime_snapshot_id"],
            "content": "Agent A cannot use Agent B provenance.",
        },
        token_a,
        expected=422,
    )
    post_1 = request(
        "POST",
        f"/api/v1/threads/{thread['thread_id']}/posts",
        {
            "runtime_snapshot_id": snapshot_a["runtime_snapshot_id"],
            "content": "Agent A posts under an authenticated session.",
        },
        token_a,
        expected=201,
    )
    post_2 = request(
        "POST",
        f"/api/v1/threads/{thread['thread_id']}/posts",
        {
            "runtime_snapshot_id": snapshot_b["runtime_snapshot_id"],
            "parent_post_id": post_1["post_id"],
            "content": "Agent B independently authenticates and replies.",
        },
        token_b,
        expected=201,
    )
    assert post_1["author_agent_id"] == agent_a["agent_id"]
    assert post_2["author_agent_id"] == agent_b["agent_id"]

    # Rotation: last-key revocation is blocked, while adding a replacement permits it.
    request(
        "POST",
        f"/api/v1/auth/keys/{agent_b['agent_key']['agent_key_id']}/revoke",
        token=token_b,
        expected=409,
    )
    private_a_new = Ed25519PrivateKey.generate()
    key_a_new = request(
        "POST",
        "/api/v1/auth/keys",
        {"public_key": public_key_b64(private_a_new), "key_label": "A rotated"},
        token_a,
        expected=201,
    )
    request(
        "POST",
        f"/api/v1/auth/keys/{agent_a['agent_key']['agent_key_id']}/revoke",
        token=token_a,
    )
    request("POST", "/api/v1/threads", {"title": "revoked"}, token_a, expected=401)
    request(
        "POST",
        "/api/v1/auth/challenge",
        {
            "agent_id": agent_a["agent_id"],
            "agent_key_id": agent_a["agent_key"]["agent_key_id"],
        },
        expected=404,
    )
    rotated_registration = {
        "agent_id": agent_a["agent_id"],
        "agent_key": key_a_new,
    }
    rotated_token, _ = authenticate(rotated_registration, private_a_new)
    expire_session(rotated_token)
    request(
        "POST", "/api/v1/threads", {"title": "expired"}, rotated_token, expected=401
    )

    detail = request("GET", f"/api/v1/threads/{thread['thread_id']}")
    assert [post["post_id"] for post in detail["posts"]] == [
        post_1["post_id"],
        post_2["post_id"],
    ]
    assert detail["posts"][1]["parent_post_id"] == post_1["post_id"]

    events = request("GET", "/api/v1/events?limit=500")
    scenario_agent_ids = {agent_a["agent_id"], agent_b["agent_id"]}
    scenario_events = [
        event for event in events if event["actor_agent_id"] in scenario_agent_ids
    ]
    assert [datetime.fromisoformat(event["created_at"]) for event in scenario_events] == sorted(
        datetime.fromisoformat(event["created_at"]) for event in scenario_events
    )
    event_dump = json.dumps(scenario_events)
    forbidden = [token_a, token_b, rotated_token, invalid_challenge["nonce"]]
    assert all(secret not in event_dump for secret in forbidden)
    assert "AGENT_KEY_ADDED" in {event["event_type"] for event in scenario_events}
    assert "AGENT_KEY_REVOKED" in {event["event_type"] for event in scenario_events}
    for agent_id in scenario_agent_ids:
        agent_event_types = [
            event["event_type"]
            for event in scenario_events
            if event["actor_agent_id"] == agent_id
        ]
        assert agent_event_types[:2] == ["AGENT_CREATED", "AGENT_KEY_ADDED"]

    print(
        json.dumps(
            {
                "status": "ok",
                "agent_a": agent_a["agent_id"],
                "agent_b": agent_b["agent_id"],
                "runtime_snapshot_a": snapshot_a["runtime_snapshot_id"],
                "runtime_snapshot_b": snapshot_b["runtime_snapshot_id"],
                "thread": thread["thread_id"],
                "post_1": post_1["post_id"],
                "post_2": post_2["post_id"],
                "security_checks": {
                    "invalid_signature": "rejected",
                    "expired_challenge": "rejected",
                    "challenge_replay": "rejected",
                    "cross_agent_impersonation": "rejected",
                    "last_active_key_revocation": "rejected",
                    "revoked_key": "rejected",
                    "revoked_key_session": "rejected",
                    "expired_session": "rejected",
                    "auth_secrets_in_events": False,
                },
                "event_types": [event["event_type"] for event in scenario_events],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
