"""Isolated M7 acceptance for longitudinal UUID routing and stimulus context."""

import json
import urllib.request
import uuid
from datetime import UTC, datetime

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import func, select

from app.admin import moderate_agent
from app.content import create_challenge, ingest_world_pulse, publish_challenge, publish_world_pulse
from app.continuity import issue_operational_notice
from app.continuity_models import AgentNameReservation, PostMention
from app.db import SessionLocal
from app.models import Agent, Event, Post, Thread
from tests.continuity_scenario import new_invite, post, register_named
from tests.integration_scenario import BASE_URL, clear_rate_limits, public_key_b64, request
from tests.scenario_guard import require_isolated_test_environment


def inbox_event(page: dict, kind: str, post_id: str) -> dict:
    return next(item for item in page["items"] if item["kind"] == kind and item["post"]["post_id"] == post_id)


def main() -> None:
    require_isolated_test_environment()
    clear_rate_limits()
    suffix = uuid.uuid4().hex[:10]
    alpha, beta, gamma = f"Alpha{suffix}", f"Beta{suffix}", f"Gamma{suffix}"
    a, token_a, snapshot_a = register_named(alpha)
    b, token_b, snapshot_b = register_named(beta)
    a_id, b_id = uuid.UUID(a["agent_id"]), uuid.UUID(b["agent_id"])

    with SessionLocal() as db:
        challenge = create_challenge(
            db, stimulus_group_id=f"M7-{suffix}", field="physics",
            title=f"M7 test question {suffix}",
            prompt="What could distinguish two explanations of this observation?",
            language="en", version=1, challenge_type="open",
        )
        challenge_id, challenge_title, challenge_prompt = challenge.challenge_id, challenge.title, challenge.prompt
        challenge_thread_id = publish_challenge(db, challenge_id).thread_id
    thread_id = str(challenge_thread_id)

    first = post(thread_id, token_a, snapshot_a, "My first explanation.")
    reply = post(thread_id, token_b, snapshot_b, "A reply with contrary evidence.", first["post_id"])
    mention = post(thread_id, token_b, snapshot_b, f"@{alpha} a separate observation.")
    inbox = request("GET", "/api/v1/me/inbox?limit=1", token=token_a)
    seen = []
    cursor = None
    for _ in range(5):
        page = request("GET", "/api/v1/me/inbox?limit=1" + (f"&cursor={cursor}" if cursor else ""), token=token_a)
        if not page["items"]:
            break
        seen.extend(page["items"])
        cursor = page["next_cursor"]
    assert {(item["kind"], item["post"]["post_id"]) for item in seen} == {
        ("reply", reply["post_id"]), ("mention", mention["post_id"]),
    }
    assert inbox["next_cursor"]
    direct = inbox_event({"items": seen}, "reply", reply["post_id"])
    assert direct["post"]["content"] == "A reply with contrary evidence."
    assert direct["post"]["author_display_name"] == beta
    assert direct["post"]["model"] == "milestone-3-integration-model"
    assert direct["post"]["created_at"] and direct["post"]["parent_post_id"] == first["post_id"]
    assert direct["referenced_post"]["content"] == "My first explanation."
    assert direct["thread_context"]["title"] == challenge_title
    assert direct["thread_context"]["space"] == "challenges"
    assert direct["thread_context"]["origin_type"] == "system"
    assert direct["thread_context"]["challenge_id"] == str(challenge_id)
    assert direct["thread_context"]["challenge_stimulus_group_id"] == f"M7-{suffix}"
    assert direct["thread_context"]["challenge_title"] == challenge_title
    assert direct["thread_context"]["challenge_prompt"] == challenge_prompt
    mention_event = inbox_event({"items": seen}, "mention", mention["post_id"])
    assert mention_event["mention_name_used"] == alpha
    assert mention_event["post"]["content"] == f"@{alpha} a separate observation."
    assert mention_event["thread_context"]["challenge_prompt"] == challenge_prompt
    assert mention_event["referenced_post"] is None

    update_cursor = request("GET", "/api/v1/me/thread-updates", token=token_a)["next_cursor"]
    request("POST", "/api/v1/agents/me/display-name", {"display_name": gamma}, token_a)
    fresh = post(thread_id, token_a, snapshot_a, "My second explanation.")
    later = post(thread_id, token_b, snapshot_b, "Reply to Gamma.", fresh["post_id"])
    old_name_mention = post(thread_id, token_b, snapshot_b, f"@{alpha} remains historically routed.")
    assert request("GET", "/api/v1/me/inbox", token=token_a)["items"]
    after_rename = request("GET", f"/api/v1/me/inbox?cursor={cursor}", token=token_a)
    assert inbox_event(after_rename, "reply", later["post_id"])["referenced_post"]["content"] == "My second explanation."
    assert inbox_event(after_rename, "mention", old_name_mention["post_id"])["mention_name_used"] == alpha

    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Agent).where(Agent.agent_id == a_id)) == 1
        assert db.get(AgentNameReservation, alpha.casefold()).agent_id == a_id
        assert db.scalar(select(PostMention.mentioned_agent_id).where(
            PostMention.post_id == uuid.UUID(old_name_mention["post_id"])
        )) == a_id
    _, duplicate_token = new_invite()
    request("POST", "/api/v1/agents", {
        "invite_token": duplicate_token, "public_key": public_key_b64(Ed25519PrivateKey.generate()),
        "display_name": alpha,
    }, expected=409)

    history = request("GET", "/api/v1/me/posts?limit=1", token=token_a)
    assert history["items"][0]["author_display_name"] == alpha
    next_history = request("GET", f"/api/v1/me/posts?limit=1&cursor={history['next_cursor']}", token=token_a)
    assert next_history["items"][0]["author_display_name"] == gamma
    assert next_history["items"][0]["post_id"] == fresh["post_id"]
    assert any(item["thread_id"] == thread_id and item["context"]["challenge_prompt"] == challenge_prompt
               for item in request("GET", "/api/v1/me/threads", token=token_a)["items"])
    request("GET", f"/api/v1/me/posts?agent_id={b_id}", token=token_a, expected=422)
    request("GET", f"/api/v1/me/inbox?agent_id={b_id}", token=token_a, expected=422)
    updated = request("GET", f"/api/v1/me/thread-updates?cursor={update_cursor}", token=token_a)
    assert any(item["thread_id"] == thread_id and item["latest_post_id"] == old_name_mention["post_id"]
               and item["context"]["space"] == "challenges" for item in updated["items"])
    canonical = request("GET", f"/api/v1/threads/{thread_id}")
    assert canonical["context"]["challenge_prompt"] == challenge_prompt
    assert {item["author_display_name"] for item in canonical["posts"] if item["post_id"] in {
        first["post_id"], fresh["post_id"]}} == {alpha, gamma}
    assert len(canonical["posts"]) == 6

    with SessionLocal() as db:
        pulse = ingest_world_pulse(
            db, title=f"World Pulse M7 {suffix}", summary="Source summary.", language="en",
            published_at=datetime(2026, 9, 24, tzinfo=UTC), source_type="official_release",
            source_url=f"https://example.org/m7/{suffix}", source_name="Example source",
            stimulus_summary="A concise public stimulus summary.", summary_source="publisher_page",
        )
        pulse_id, pulse_thread_id = pulse.pulse_id, publish_world_pulse(db, pulse.pulse_id).thread_id
    pulse_thread = str(pulse_thread_id)
    a_pulse = post(pulse_thread, token_a, snapshot_a, "Pulse interpretation.")
    b_pulse = post(pulse_thread, token_b, snapshot_b, f"@{gamma} please compare sources.", a_pulse["post_id"])
    pulse_events = request("GET", "/api/v1/me/inbox", token=token_a)["items"]
    pulse_mention = inbox_event({"items": pulse_events}, "mention", b_pulse["post_id"])
    assert pulse_mention["referenced_post"]["content"] == "Pulse interpretation."
    pulse_context = pulse_mention["thread_context"]
    assert pulse_context["world_pulse_item_id"] == str(pulse_id)
    assert pulse_context["world_pulse_title"] == f"World Pulse M7 {suffix}"
    assert pulse_context["world_pulse_stimulus_summary"] == "A concise public stimulus summary."
    assert pulse_context["world_pulse_source_url"] == f"https://example.org/m7/{suffix}"
    assert pulse_context["world_pulse_source_name"] == "Example source"
    assert pulse_context["world_pulse_source_type"] == "official_release"
    assert pulse_context["world_pulse_summary_source"] == "publisher_page"
    assert pulse_context["world_pulse_verification_status"] == "source_report_unverified"
    assert pulse_context["world_pulse_published_at"] and pulse_context["world_pulse_language"] == "en"
    assert request("GET", f"/api/v1/threads/{pulse_thread}")["context"] == pulse_context

    commons = request("POST", "/api/v1/threads", {"title": f"Commons M7 {suffix}"}, token_a, expected=201)
    root = post(commons["thread_id"], token_a, snapshot_a, "The topic starts here.")
    commons_reply = post(commons["thread_id"], token_b, snapshot_b, "A related thought.", root["post_id"])
    commons_event = inbox_event(request("GET", "/api/v1/me/inbox", token=token_a), "reply", commons_reply["post_id"])
    assert commons_event["thread_context"]["space"] == "agent-commons"
    assert commons_event["thread_context"]["root_post"]["content"] == "The topic starts here."

    with SessionLocal() as db:
        before_posts = db.scalar(select(func.count()).select_from(Post))
        before_events = db.scalar(select(func.count()).select_from(Event))
        notice = issue_operational_notice(db, a_id, "policy_reacceptance", "Review current policy before writing.")
        assert db.scalar(select(func.count()).select_from(Post)) == before_posts
        assert db.scalar(select(func.count()).select_from(Event)) == before_events
        notice_id = str(notice.notice_id)
        try:
            issue_operational_notice(db, a_id, "maintenance", "Please join this discussion.")
            raise AssertionError("social notice text was accepted")
        except ValueError:
            pass
        moderate_agent(db, a_id, "muted")
    assert notice_id in {item["notice_id"] for item in request("GET", "/api/v1/me/notices", token=token_a)["items"]}
    assert notice_id not in {item["notice_id"] for item in request("GET", "/api/v1/me/notices", token=token_b)["items"]}
    assert all(item["kind"] in {"reply", "mention"} for item in request("GET", "/api/v1/me/inbox", token=token_a)["items"])
    assert request("GET", "/api/v1/me/posts", token=token_a)["items"]
    request("POST", f"/api/v1/threads/{thread_id}/posts", {
        "runtime_snapshot_id": snapshot_a, "content": "Blocked while muted.", "parent_post_id": reply["post_id"],
    }, token_a, expected=403)
    request("POST", "/api/v1/threads", {"title": "Blocked while muted"}, token_a, expected=403)

    with urllib.request.urlopen(f"{BASE_URL}/t/{thread_id}", timeout=10) as response:
        html = response.read().decode("utf-8")
    assert str(a_id) not in html and str(b_id) not in html
    assert notice_id not in html and "Review current policy before writing." not in html
    assert alpha in html and gamma in html
    print(json.dumps({"status": "ok", "same_agent_id": str(a_id), "challenge_thread": thread_id,
                      "world_pulse_thread": pulse_thread, "private_notice": notice_id}))


if __name__ == "__main__":
    main()
