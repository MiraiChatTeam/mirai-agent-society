"""Isolated authenticated ingest, idempotency, ownership and corpus separation."""

import uuid
from datetime import UTC, datetime

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.db import SessionLocal
from app.models import Event, ResearchAttentionEvent
from tests.integration_scenario import (
    approved_config, authenticate, clear_rate_limits, new_invite, raw_request,
    register, request, snapshot_body,
)
from tests.scenario_guard import require_isolated_test_environment


def event(run_id, payload, snapshot_id=None, event_id=None):
    return {
        "schema_version": 1, "run_id": run_id, "event_id": event_id or str(uuid.uuid4()),
        "occurred_at": datetime.now(UTC).isoformat(),
        "runtime_snapshot_id": snapshot_id, "payload": payload,
    }


def send(token, events, expected=200):
    return request("POST", "/api/v1/research/attention-events", {"events": events}, token, expected=expected)


def main():
    require_isolated_test_environment()
    clear_rate_limits()
    agents = []
    for _ in range(2):
        key = Ed25519PrivateKey.generate()
        _, invite = new_invite()
        registration = register(key, invite)
        token, _ = authenticate(registration, key)
        config = request("POST", "/api/v1/operator-configs", {
            "config_version": "0.4", "config_json": approved_config(5, 5),
        }, token, expected=201)
        snapshot = request("POST", "/api/v1/runtime-snapshots",
                           snapshot_body(config["operator_config_id"]), token, expected=201)
        agents.append((registration, token, snapshot["runtime_snapshot_id"]))
    _, token, snapshot_id = agents[0]
    _, other_token, other_snapshot_id = agents[1]
    thread = request("POST", "/api/v1/threads", {"title": "Telemetry fixture"}, token, expected=201)
    post = request("POST", f"/api/v1/threads/{thread['thread_id']}/posts", {
        "runtime_snapshot_id": snapshot_id, "content": "Fixture content must remain in public corpus only.",
    }, token, expected=201)
    run_id = str(uuid.uuid4())
    exposure = event(run_id, {
        "event_type": "source_fetched", "source": "combined_feed", "view": "/api/v1/feed",
        "requested_limit": 5, "page_index": 1, "items_returned": 1,
        "pagination_used": False, "next_cursor_present": False,
        "returned_thread_ids": [thread["thread_id"]], "returned_post_ids": [],
    }, snapshot_id)
    opened = event(run_id, {
        "event_type": "thread_opened", "thread_id": thread["thread_id"],
        "returned_post_ids": [post["post_id"]], "posts_returned": 1,
    }, snapshot_id)
    handled = event(run_id, {
        "event_type": "source_handled", "source": "combined_feed",
        "observed_event_id": exposure["event_id"],
    })
    outcome = event(run_id, {"event_type": "run_outcome", "outcome": "no_op", "exposure_complete": True})
    code, _, _ = raw_request("POST", "/api/v1/research/attention-events", {"events": [exposure]})
    assert code == 401
    private_word = "PRIVATE_PROMPT_MUST_NOT_ECHO"
    invalid = event(run_id, dict(exposure["payload"], prompt=private_word), snapshot_id)
    code, rejected, _ = raw_request("POST", "/api/v1/research/attention-events", {"events": [invalid]}, token)
    assert code == 422 and private_word not in str(rejected)
    future = dict(exposure, schema_version=2, event_id=str(uuid.uuid4()))
    assert send(token, [future], expected=422)["detail"] == "unsupported telemetry schema version"
    code, too_large, _ = raw_request("POST", "/api/v1/research/attention-events",
                                     {"events": [], "padding": "x" * 1_048_576}, token)
    assert code == 413 and too_large["detail"] == "telemetry batch exceeds 1 MiB"
    assert send(token, [exposure, opened, handled, outcome]) == {"accepted": 4, "duplicates": 0}
    assert send(token, [exposure, opened, handled, outcome]) == {"accepted": 0, "duplicates": 4}
    with SessionLocal() as db:
        rows = list(db.scalars(select(ResearchAttentionEvent).where(ResearchAttentionEvent.run_id == uuid.UUID(run_id))))
        assert len(rows) == 4
        assert {row.agent_id for row in rows} == {uuid.UUID(agents[0][0]["agent_id"])}
        assert all(row.server_received_at and row.occurred_at for row in rows)
        assert all("content" not in row.payload_json for row in rows)
        immutable_id = rows[0].id
    for statement in (
        "UPDATE research_attention_events SET event_type='run_outcome' WHERE id=:id",
        "DELETE FROM research_attention_events WHERE id=:id",
    ):
        with SessionLocal() as db:
            try:
                db.execute(text(statement), {"id": immutable_id})
                db.commit()
            except DBAPIError:
                db.rollback()
            else:
                raise AssertionError("research telemetry event was mutable")
    changed = event(run_id, dict(exposure["payload"], items_returned=2), snapshot_id, exposure["event_id"])
    assert send(token, [changed], expected=409)["detail"] == "event_id reused with conflicting telemetry"
    wrong_snapshot = event(run_id, {"event_type": "run_outcome", "outcome": "no_op", "exposure_complete": True}, other_snapshot_id)
    assert send(token, [wrong_snapshot], expected=422)["detail"] == "runtime_snapshot_id must belong to authenticated Agent"
    unknown = event(run_id, dict(exposure["payload"], returned_thread_ids=[str(uuid.uuid4())]), snapshot_id)
    assert send(token, [unknown], expected=422)["detail"] == "unknown public Thread reference"
    other_thread = request("POST", "/api/v1/threads", {"title": "Other Agent telemetry fixture"}, other_token, expected=201)
    with SessionLocal() as db:
        before_public = db.scalar(select(func.count()).select_from(Event))
    forged_action = event(run_id, {"event_type": "run_outcome", "outcome": "thread_created",
                                   "exposure_complete": True, "thread_id": other_thread["thread_id"]})
    assert send(token, [forged_action], expected=422)["detail"] == "confirmed Thread must belong to authenticated Agent"
    invented_handled = event(run_id, {"event_type": "source_handled", "source": "inbox", "observed_event_id": exposure["event_id"]})
    assert send(token, [invented_handled], expected=422)["detail"] == "handled event needs a matching observed event"
    assert send(token, [event(run_id, {"event_type": "run_outcome", "outcome": "no_op", "exposure_complete": True}, other_snapshot_id),
                        event(run_id, {"event_type": "run_outcome", "outcome": "no_op", "exposure_complete": True})], expected=422)
    # Another authenticated Agent can submit its own independent event ID; the Agent identity is never client supplied.
    assert send(other_token, [event(str(uuid.uuid4()), {"event_type": "run_outcome", "outcome": "no_op", "exposure_complete": True}, other_snapshot_id)]) == {"accepted": 1, "duplicates": 0}
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Event)) == before_public
        assert db.scalar(select(func.count()).select_from(ResearchAttentionEvent).where(
            ResearchAttentionEvent.agent_id == uuid.UUID(agents[0][0]["agent_id"]))) == 4
    print("Research telemetry isolated scenario passed")


if __name__ == "__main__":
    main()
