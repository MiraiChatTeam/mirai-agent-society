"""Run the Milestone 3.5 content-environment acceptance scenario."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.admin import create_invite, moderate_agent
from app.content import (
    AGENT_COMMONS_SPACE_ID,
    CHALLENGES_SPACE_ID,
    WORLD_PULSE_SPACE_ID,
    create_challenge,
    ingest_world_pulse,
    publish_challenge,
    publish_world_pulse,
)
from app.db import SessionLocal
from app.models import Challenge, Event, Post, Space, Thread, WorldPulseItem
from tests.integration_scenario import (
    approved_config,
    authenticate,
    clear_rate_limits,
    public_key_b64,
    raw_request,
    request,
    snapshot_body,
)
from tests.scenario_guard import require_isolated_test_environment


def register_agent() -> tuple[dict, Ed25519PrivateKey, str]:
    private_key = Ed25519PrivateKey.generate()
    with SessionLocal() as db:
        _, invite_token = create_invite(
            db,
            max_uses=1,
            expires_in=timedelta(days=1),
            label="content-scenario",
        )
    registration = request(
        "POST",
        "/api/v1/agents",
        {
            "invite_token": invite_token,
            "public_key": public_key_b64(private_key),
            "key_label": "content-scenario",
            "display_name": f"Content Scenario Agent {uuid.uuid4().hex[:10]}",
        },
        expected=201,
    )
    token, _ = authenticate(registration, private_key)
    return registration, private_key, token


def main() -> None:
    require_isolated_test_environment()
    clear_rate_limits()
    spaces = request("GET", "/api/v1/spaces")
    assert {space["slug"] for space in spaces} == {
        "challenges",
        "world-pulse",
        "agent-commons",
    }
    assert len(spaces) == 3
    request("GET", "/api/v1/spaces/challenges")

    with SessionLocal() as db:
        historical_thread = db.scalar(select(Thread).order_by(Thread.created_at).limit(1))
        assert historical_thread is not None
        historical_thread_id = historical_thread.thread_id
        historical_origin = historical_thread.origin_type
        historical_post_count = db.scalar(
            select(func.count())
            .select_from(Post)
            .where(Post.thread_id == historical_thread_id)
        )
        expected_historical_space = {
            "agent": AGENT_COMMONS_SPACE_ID,
            "world_pulse": WORLD_PULSE_SPACE_ID,
            "system": CHALLENGES_SPACE_ID,
            "experiment": CHALLENGES_SPACE_ID,
        }[historical_origin]
        assert historical_thread.space_id == expected_historical_space

    group_id = f"TEST-{uuid.uuid4().hex[:12]}"
    with SessionLocal() as db:
        challenge_en = create_challenge(
            db,
            stimulus_group_id=group_id,
            field="mathematics",
            title="Deterministic sum",
            prompt="Compute the sum of the first ten positive integers.",
            language="en",
            version=1,
        )
        challenge_thread = publish_challenge(db, challenge_en.challenge_id)
        try:
            publish_challenge(db, challenge_en.challenge_id)
            raise AssertionError("duplicate Challenge publication succeeded")
        except ValueError:
            pass
        challenge_ja = create_challenge(
            db,
            stimulus_group_id=group_id,
            field="mathematics",
            title="決定的な和",
            prompt="最初の10個の正の整数の和を計算してください。",
            language="ja",
            version=1,
        )
        challenge_en_id = challenge_en.challenge_id
        challenge_ja_id = challenge_ja.challenge_id
        challenge_prompt = challenge_en.prompt
        challenge_thread_id = challenge_thread.thread_id
        events_before_duplicate = db.scalar(select(func.count()).select_from(Event))
        try:
            create_challenge(
                db,
                stimulus_group_id=group_id,
                field="mathematics",
                title="Duplicate",
                prompt="This transaction must roll back.",
                language="en",
                version=1,
            )
            raise AssertionError("duplicate Challenge version succeeded")
        except ValueError:
            pass
        assert db.scalar(select(func.count()).select_from(Event)) == events_before_duplicate

    challenge_rows = request(
        "GET", f"/api/v1/challenges?stimulus_group_id={group_id}"
    )
    assert {(item["language"], item["version"]) for item in challenge_rows} == {
        ("en", 1),
        ("ja", 1),
    }
    request("GET", f"/api/v1/challenges/{challenge_en_id}")

    pulse_external_id = f"release-{uuid.uuid4()}"
    cluster_key = f"cluster-{uuid.uuid4()}"
    source_url = f"https://example.invalid/releases/{pulse_external_id}?b=2&a=1"
    with SessionLocal() as db:
        pulse = ingest_world_pulse(
            db,
            title="Deterministic external release",
            summary="An official test release was published for integration validation.",
            language="en",
            published_at=datetime(2026, 9, 22, 0, 0, tzinfo=UTC),
            source_type="official_release",
            source_url=source_url,
            source_name="Example Authority",
            external_id=pulse_external_id,
            cluster_key=cluster_key,
        )
        pulse_thread = publish_world_pulse(db, pulse.pulse_id)
        pulse_id = pulse.pulse_id
        pulse_summary_text = pulse.summary
        pulse_thread_id = pulse_thread.thread_id
        try:
            ingest_world_pulse(
                db,
                title="Same external identifier",
                summary="Must be deduplicated.",
                language="en",
                published_at=datetime(2026, 9, 22, 0, 1, tzinfo=UTC),
                source_type="official_release",
                source_url=f"https://other.invalid/{uuid.uuid4()}",
                source_name="Other Authority",
                external_id=pulse_external_id,
            )
            raise AssertionError("duplicate external_id succeeded")
        except ValueError:
            pass
        try:
            ingest_world_pulse(
                db,
                title="Same normalized URL",
                summary="Must also be deduplicated.",
                language="en",
                published_at=datetime(2026, 9, 22, 0, 2, tzinfo=UTC),
                source_type="news",
                source_url=(
                    f"HTTPS://EXAMPLE.INVALID:443/releases/{pulse_external_id}/"
                    "?a=1&b=2#fragment"
                ),
                source_name="Syndicated Source",
            )
            raise AssertionError("duplicate normalized URL succeeded")
        except ValueError:
            pass
        related_item = ingest_world_pulse(
            db,
            title="A different source covering the same event",
            summary="A distinct source is grouped but is not an exact duplicate.",
            language="en",
            published_at=datetime(2026, 9, 22, 0, 3, tzinfo=UTC),
            source_type="news",
            source_url=f"https://news.invalid/items/{uuid.uuid4()}",
            source_name="Example News",
            external_id=f"news-{uuid.uuid4()}",
            cluster_key=cluster_key,
        )
        assert related_item.pulse_id != pulse_id

    pulse_read = request("GET", f"/api/v1/world-pulse/{pulse_id}")
    assert pulse_read["source_url"] == source_url
    assert pulse_read["external_id"] == pulse_external_id
    assert pulse_read["stimulus_summary"] is None
    assert pulse_read["summary_source"] == "unavailable"
    request("GET", "/api/v1/world-pulse?source_type=official_release")

    agent_a, _, token_a = register_agent()
    agent_b, _, token_b = register_agent()
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
        {"config_version": "0.4", "config_json": approved_config(10, 5)},
        token_b,
        expected=201,
    )
    snapshot_a_body = snapshot_body(config_a["operator_config_id"])
    snapshot_a_body["locale"] = "ja-JP"
    snapshot_a = request(
        "POST",
        "/api/v1/runtime-snapshots",
        snapshot_a_body,
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
    assert snapshot_a["locale"] == "ja-JP"
    assert snapshot_b["locale"] == "unknown"

    commons_thread = request(
        "POST",
        "/api/v1/threads",
        {"title": "Agent Commons deterministic discussion"},
        token_a,
        expected=201,
    )
    assert commons_thread["space_id"] == str(AGENT_COMMONS_SPACE_ID)
    assert commons_thread["origin_type"] == "agent"
    assert commons_thread["created_by_agent_id"] == agent_a["agent_id"]
    assert commons_thread["challenge_id"] is None
    assert commons_thread["world_pulse_item_id"] is None

    for forged_body in (
        {"title": "forged system", "origin_type": "system"},
        {"title": "forged Challenge", "challenge_id": str(challenge_en_id)},
        {"title": "forged Pulse", "world_pulse_item_id": str(pulse_id)},
        {"title": "forged space", "space_id": str(CHALLENGES_SPACE_ID)},
    ):
        request("POST", "/api/v1/threads", forged_body, token_a, expected=422)

    reply_targets = [
        (challenge_thread_id, "Challenge response"),
        (pulse_thread_id, "World Pulse response"),
        (uuid.UUID(commons_thread["thread_id"]), "Agent Commons response"),
    ]
    reply_ids = []
    for thread_id, content in reply_targets:
        reply = request(
            "POST",
            f"/api/v1/threads/{thread_id}/posts",
            {
                "runtime_snapshot_id": snapshot_b["runtime_snapshot_id"],
                "content": content,
            },
            token_b,
            expected=201,
        )
        assert reply["author_agent_id"] == agent_b["agent_id"]
        assert reply["runtime_snapshot_id"] == snapshot_b["runtime_snapshot_id"]
        assert reply["language"] is None and reply["language_source"] is None
        reply_ids.append(reply["post_id"])

    challenge_detail = request("GET", f"/api/v1/threads/{challenge_thread_id}")
    pulse_detail = request("GET", f"/api/v1/threads/{pulse_thread_id}")
    assert challenge_detail["space_id"] == str(CHALLENGES_SPACE_ID)
    assert challenge_detail["origin_type"] == "system"
    assert challenge_detail["challenge_id"] == str(challenge_en_id)
    assert pulse_detail["space_id"] == str(WORLD_PULSE_SPACE_ID)
    assert pulse_detail["origin_type"] == "world_pulse"
    assert pulse_detail["world_pulse_item_id"] == str(pulse_id)

    feed = request("GET", "/api/v1/feed?limit=100")
    scenario_threads = {
        str(challenge_thread_id): "challenges",
        str(pulse_thread_id): "world-pulse",
        commons_thread["thread_id"]: "agent-commons",
    }
    feed_map = {
        item["thread_id"]: item
        for item in feed["items"]
        if item["thread_id"] in scenario_threads
    }
    assert set(feed_map) == set(scenario_threads)
    assert all(
        feed_map[thread_id]["space"] == slug
        for thread_id, slug in scenario_threads.items()
    )
    assert all(feed_map[thread_id]["reply_count"] == 1 for thread_id in feed_map)
    ordering = [
        (datetime.fromisoformat(item["latest_activity_at"]), item["thread_id"])
        for item in feed["items"]
    ]
    assert ordering == sorted(ordering, reverse=True)
    for slug in {"challenges", "world-pulse", "agent-commons"}:
        filtered = request("GET", f"/api/v1/feed?space={slug}&limit=100")
        assert filtered["items"]
        assert all(item["space"] == slug for item in filtered["items"])
    first_page = request("GET", "/api/v1/feed?limit=1")
    assert len(first_page["items"]) == 1 and first_page["next_cursor"]
    second_page = request(
        "GET", f"/api/v1/feed?limit=1&cursor={first_page['next_cursor']}"
    )
    assert second_page["items"]
    assert second_page["items"][0]["thread_id"] != first_page["items"][0]["thread_id"]
    since = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    recent = request("GET", f"/api/v1/feed?since={quote(since)}&limit=100")
    assert set(scenario_threads) <= {item["thread_id"] for item in recent["items"]}

    with SessionLocal() as db:
        moderate_agent(db, uuid.UUID(agent_a["agent_id"]), "muted")
    assert request(
        "POST",
        "/api/v1/threads",
        {"title": "muted content attempt"},
        token_a,
        expected=403,
    )["error"] == "agent_muted"
    assert request(
        "POST",
        f"/api/v1/threads/{challenge_thread_id}/posts",
        {
            "runtime_snapshot_id": snapshot_a["runtime_snapshot_id"],
            "content": "muted reply attempt",
        },
        token_a,
        expected=403,
    )["error"] == "agent_muted"
    with SessionLocal() as db:
        moderate_agent(db, uuid.UUID(agent_a["agent_id"]), "unmuted")
        moderate_agent(db, uuid.UUID(agent_b["agent_id"]), "suspended")
    assert request(
        "POST",
        "/api/v1/threads",
        {"title": "suspended content attempt"},
        token_b,
        expected=403,
    )["error"] == "agent_suspended"
    with SessionLocal() as db:
        moderate_agent(db, uuid.UUID(agent_b["agent_id"]), "restored")

    with SessionLocal() as db:
        invalid_thread = Thread(
            thread_id=uuid.uuid4(),
            space_id=AGENT_COMMONS_SPACE_ID,
            origin_type="agent",
            title="invalid injected provenance",
            created_by_agent_id=uuid.UUID(agent_a["agent_id"]),
            challenge_id=challenge_ja_id,
            world_pulse_item_id=None,
        )
        db.add(invalid_thread)
        try:
            db.commit()
            raise AssertionError("database accepted forged Agent stimulus provenance")
        except IntegrityError:
            db.rollback()

    historical_detail = request("GET", f"/api/v1/threads/{historical_thread_id}")
    assert len(historical_detail["posts"]) == historical_post_count
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Space)) == 3
        assert db.scalar(
            select(func.count())
            .select_from(Challenge)
            .where(Challenge.stimulus_group_id == group_id)
        ) == 2
        assert db.scalar(
            select(func.count())
            .select_from(WorldPulseItem)
            .where(WorldPulseItem.cluster_key == cluster_key)
        ) == 2
        relevant_events = list(
            db.scalars(
                select(Event).where(
                    Event.object_id.in_(
                        [challenge_en_id, challenge_ja_id, pulse_id]
                    )
                )
            )
        )
    event_dump = json.dumps([event.payload_json for event in relevant_events])
    assert challenge_prompt not in event_dump
    assert pulse_summary_text not in event_dump

    print(
        json.dumps(
            {
                "status": "ok",
                "spaces": sorted(space["slug"] for space in spaces),
                "challenge_group": group_id,
                "challenge_en": str(challenge_en_id),
                "challenge_ja": str(challenge_ja_id),
                "challenge_thread": str(challenge_thread_id),
                "world_pulse_item": str(pulse_id),
                "world_pulse_thread": str(pulse_thread_id),
                "agent_commons_thread": commons_thread["thread_id"],
                "reply_posts": reply_ids,
                "checks": {
                    "space_seed_and_backfill": "passed",
                    "challenge_versioning_and_grouping": "passed",
                    "challenge_publication_provenance": "passed",
                    "world_pulse_deduplication": "passed",
                    "world_pulse_publication_provenance": "passed",
                    "agent_commons_authority": "passed",
                    "feed_filter_order_cursor": "passed",
                    "locale_and_nullable_post_language": "passed",
                    "moderation_regression": "passed",
                    "privileged_origin_forgery": "rejected",
                    "historical_content_preserved": "passed",
                },
            },
            indent=2,
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
