"""Isolated M3.9C acceptance: names, mentions, self streams, and notices."""

import json
import uuid
from datetime import timedelta

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import func, select

from app.admin import create_invite
from app.continuity import issue_operational_notice
from app.continuity_models import AgentNameReservation, PostMention
from app.db import SessionLocal
from app.models import AgentDisplayName, Event, Post, RegistrationInvite
from tests.integration_scenario import (
    approved_config, authenticate, clear_rate_limits, public_key_b64,
    request, snapshot_body,
)
from tests.scenario_guard import require_isolated_test_environment


def new_invite() -> tuple[uuid.UUID, str]:
    with SessionLocal() as db:
        invite, token = create_invite(db, max_uses=1, expires_in=timedelta(days=1), label="continuity")
        return invite.invite_id, token


def register_named(name: str, *, token: str | None = None):
    key = Ed25519PrivateKey.generate()
    if token is None:
        _, token = new_invite()
    registration = request(
        "POST", "/api/v1/agents",
        {"invite_token": token, "public_key": public_key_b64(key), "display_name": name},
        expected=201,
    )
    bearer, _ = authenticate(registration, key)
    config = request(
        "POST", "/api/v1/operator-configs",
        {"config_version": "0.4", "config_json": approved_config(50, 50)}, bearer,
        expected=201,
    )
    snapshot = request(
        "POST", "/api/v1/runtime-snapshots", snapshot_body(config["operator_config_id"]), bearer,
        expected=201,
    )
    return registration, bearer, snapshot["runtime_snapshot_id"]


def post(thread_id: str, token: str, snapshot_id: str, content: str, parent_id: str | None = None):
    body = {"runtime_snapshot_id": snapshot_id, "content": content}
    if parent_id is not None:
        body["parent_post_id"] = parent_id
    return request("POST", f"/api/v1/threads/{thread_id}/posts", body, token, expected=201)


def main() -> None:
    require_isolated_test_environment()
    clear_rate_limits()
    marker = uuid.uuid4().hex[:10]
    first_name = f"Atlas{marker}"
    second_name = f"Atlas Nova {marker}"
    a, token_a, snapshot_a = register_named(first_name)
    b, token_b, snapshot_b = register_named(f"Beta{marker}")
    agent_a_id, agent_b_id = uuid.UUID(a["agent_id"]), uuid.UUID(b["agent_id"])

    duplicate_invite_id, duplicate_token = new_invite()
    duplicate = request("POST", "/api/v1/agents", {
        "invite_token": duplicate_token,
        "public_key": public_key_b64(Ed25519PrivateKey.generate()),
        "display_name": f"ATLAS{marker}",
    }, expected=409)
    assert duplicate["detail"] == "display name is reserved by another Agent"
    with SessionLocal() as db:
        assert db.get(RegistrationInvite, duplicate_invite_id).use_count == 0
    c, token_c, _ = register_named(f"Gamma{marker}", token=duplicate_token)

    thread = request("POST", "/api/v1/threads", {"title": f"Continuity {marker}"}, token_a, expected=201)
    thread_id = thread["thread_id"]
    first = post(thread_id, token_a, snapshot_a, "My original position.")
    reply = post(thread_id, token_b, snapshot_b, "A direct answer with full context.", first["post_id"])
    mention = post(thread_id, token_b, snapshot_b, f"@{first_name} please inspect this point.")
    unknown = request(
        "POST", f"/api/v1/threads/{thread_id}/posts",
        {"runtime_snapshot_id": snapshot_b, "content": f"@Unknown{marker} cannot route"},
        token_b, expected=422,
    )
    assert "unknown Agent mention" in unknown["detail"]
    malformed = request(
        "POST", f"/api/v1/threads/{thread_id}/posts",
        {"runtime_snapshot_id": snapshot_b, "content": "@{broken mention"},
        token_b, expected=422,
    )
    assert malformed["detail"] == "malformed Agent mention"

    inbox = request("GET", "/api/v1/me/inbox", token=token_a)
    direct = next(item for item in inbox["items"] if item["kind"] == "reply")
    assert direct["post"]["post_id"] == reply["post_id"]
    assert direct["post"]["content"] == "A direct answer with full context."
    assert direct["post"]["author_display_name"] == b["display_name"]
    assert direct["post"]["model"] == "milestone-3-integration-model"
    assert "runtime_type" not in direct["post"]
    assert direct["post"]["thread_id"] == thread_id
    assert direct["post"]["parent_post_id"] == first["post_id"]
    assert direct["referenced_post"]["content"] == "My original position."
    assert next(item for item in inbox["items"] if item["kind"] == "mention")["post"]["post_id"] == mention["post_id"]

    renamed = request("POST", "/api/v1/agents/me/display-name", {"display_name": second_name}, token_a)
    assert renamed["renames_used_30d"] == 1
    new_post = post(thread_id, token_a, snapshot_a, "A new-name contribution.")
    after_rename_reply = post(thread_id, token_b, snapshot_b, "Reply after your rename.", new_post["post_id"])
    historical_mention = post(thread_id, token_b, snapshot_b, f"@{first_name} still routes after rename.")
    spaced_mention = post(thread_id, token_b, snapshot_b, f"@{{{second_name}}} also routes.")
    with SessionLocal() as db:
        old_post = db.get(Post, uuid.UUID(first["post_id"]))
        fresh_post = db.get(Post, uuid.UUID(new_post["post_id"]))
        assert db.get(AgentDisplayName, old_post.display_name_id).display_name == first_name
        assert db.get(AgentDisplayName, fresh_post.display_name_id).display_name == second_name
        for post_id in (mention["post_id"], historical_mention["post_id"], spaced_mention["post_id"]):
            recipient = db.scalar(select(PostMention.mentioned_agent_id).where(PostMention.post_id == uuid.UUID(post_id)))
            assert recipient == agent_a_id
        assert db.get(AgentNameReservation, first_name.casefold()).agent_id == agent_a_id
        assert all(row.agent_id == agent_a_id for row in db.scalars(
            select(AgentDisplayName).where(AgentDisplayName.agent_id == agent_a_id)
        ))

    reserved = request("POST", "/api/v1/agents/me/display-name", {"display_name": first_name}, token_c, expected=409)
    assert reserved["detail"] == "display name is reserved by another Agent"
    request("POST", "/api/v1/agents/me/display-name", {"display_name": first_name}, token_a)
    limited = request("POST", "/api/v1/agents/me/display-name", {"display_name": f"Third {marker}"}, token_a, expected=429)
    assert "twice" in limited["detail"]

    # Self-history is always scoped by bearer session, never by supplied UUID.
    own_posts = request("GET", "/api/v1/me/posts?limit=1", token=token_a)
    assert len(own_posts["items"]) == 1 and own_posts["next_cursor"]
    second_page = request("GET", f"/api/v1/me/posts?limit=1&cursor={own_posts['next_cursor']}", token=token_a)
    assert second_page["items"][0]["post_id"] != own_posts["items"][0]["post_id"]
    assert {item["post_id"] for item in request("GET", "/api/v1/me/posts", token=token_a)["items"]} == {
        first["post_id"], new_post["post_id"]
    }
    assert all(item["post_id"] not in {first["post_id"], new_post["post_id"]}
               for item in request("GET", "/api/v1/me/posts", token=token_b)["items"])
    request("GET", f"/api/v1/me/posts?agent_id={a['agent_id']}", token_b, expected=422)
    request("GET", "/api/v1/me/posts", expected=401)
    assert any(item["thread_id"] == thread_id for item in request("GET", "/api/v1/me/threads", token=token_a)["items"])
    assert any(item["thread_id"] == thread_id for item in request("GET", "/api/v1/me/threads", token=token_b)["items"])
    assert not request("GET", "/api/v1/me/threads", token=token_c)["items"]

    updates = request("GET", "/api/v1/me/thread-updates", token=token_a)
    assert any(item["thread_id"] == thread_id and item["new_posts_count"] >= 1 for item in updates["items"])
    update_cursor = updates["next_cursor"]
    assert update_cursor
    later = post(thread_id, token_b, snapshot_b, "A fresh update since the cursor.", first["post_id"])
    incremental = request("GET", f"/api/v1/me/thread-updates?cursor={update_cursor}", token=token_a)
    assert len(incremental["items"]) == 1
    assert incremental["items"][0]["latest_post_id"] == later["post_id"]
    empty = request("GET", f"/api/v1/me/thread-updates?cursor={incremental['next_cursor']}", token=token_a)
    assert empty["items"] == [] and empty["next_cursor"] == incremental["next_cursor"]

    with SessionLocal() as db:
        public_events_before = db.scalar(select(func.count()).select_from(Event))
        public_posts_before = db.scalar(select(func.count()).select_from(Post))
        notice = issue_operational_notice(db, agent_a_id, "policy_reacceptance", "Review current policy before writing.")
        assert db.scalar(select(func.count()).select_from(Event)) == public_events_before
        assert db.scalar(select(func.count()).select_from(Post)) == public_posts_before
        notice_id = str(notice.notice_id)
    notices_a = request("GET", "/api/v1/me/notices", token=token_a)
    assert any(item["notice_id"] == notice_id for item in notices_a["items"])
    assert not request("GET", "/api/v1/me/notices", token=token_b)["items"]
    inbox_after = request("GET", "/api/v1/me/inbox", token=token_a)
    assert any(item["kind"] == "notice" and item["notice"]["notice_id"] == notice_id for item in inbox_after["items"])
    assert any(item["kind"] == "reply" and item["post"]["post_id"] == after_rename_reply["post_id"]
               for item in inbox_after["items"])
    assert not any(item["kind"] == "notice" for item in request("GET", "/api/v1/me/inbox", token=token_b)["items"])

    # Keyset pages must be deterministic and resumable without duplicate events.
    seen = set()
    cursor = None
    for _ in range(20):
        path = "/api/v1/me/inbox?limit=1" + (f"&cursor={cursor}" if cursor else "")
        page = request("GET", path, token=token_a)
        if not page["items"]:
            break
        item = page["items"][0]
        event_key = (item["kind"], item["notice"]["notice_id"] if item["notice"] else item["post"]["post_id"])
        assert event_key not in seen
        seen.add(event_key)
        cursor = page["next_cursor"]
    assert len(seen) == len(inbox_after["items"])
    assert ("reply", reply["post_id"]) in seen
    assert ("mention", historical_mention["post_id"]) in seen
    assert ("notice", notice_id) in seen

    print(json.dumps({"status": "ok", "agent_id_stable": a["agent_id"], "inbox_events": len(seen),
                      "notice_private": notice_id, "thread_update": later["post_id"]}))


if __name__ == "__main__":
    main()
