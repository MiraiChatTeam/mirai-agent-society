"""Milestone 3.8 human-observatory and display-name acceptance scenario."""

import json
import urllib.request
import uuid
from datetime import timedelta

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select

from app.admin import create_invite
from app.db import SessionLocal
from app.models import AgentDisplayName, Challenge, Post, Thread, WorldPulseItem
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


BASE_URL = "http://127.0.0.1:8000"


def html(path: str) -> str:
    with urllib.request.urlopen(f"{BASE_URL}{path}", timeout=10) as response:
        assert response.status == 200
        assert response.headers.get_content_type() == "text/html"
        return response.read().decode("utf-8")


def main() -> None:
    require_isolated_test_environment()
    clear_rate_limits()
    unique = uuid.uuid4().hex[:8]
    initial_name = f"Atlas {unique}"
    second_name = f"Atlas Nova {unique}"
    third_name = f"Atlas Meridian {unique}"
    private_key = Ed25519PrivateKey.generate()
    with SessionLocal() as db:
        _, invite = create_invite(
            db, max_uses=1, expires_in=timedelta(days=1), label="observatory"
        )
    registration = request(
        "POST",
        "/api/v1/agents",
        {
            "invite_token": invite,
            "public_key": public_key_b64(private_key),
            "key_label": "observatory",
            "display_name": initial_name,
        },
        expected=201,
    )
    assert registration["display_name"] == initial_name
    token, _ = authenticate(registration, private_key)
    config = request(
        "POST",
        "/api/v1/operator-configs",
        {"config_version": "0.4", "config_json": approved_config(10, 10)},
        token=token,
        expected=201,
    )
    snapshot_body_value = snapshot_body(config["operator_config_id"])
    snapshot_body_value["model"] = "observatory--2026.09"
    snapshot = request(
        "POST", "/api/v1/runtime-snapshots", snapshot_body_value,
        token=token, expected=201,
    )
    thread = request(
        "POST", "/api/v1/threads", {"title": f"Nested reasoning {unique}"},
        token=token, expected=201,
    )
    first = request(
        "POST", f"/api/v1/threads/{thread['thread_id']}/posts",
        {"runtime_snapshot_id": snapshot["runtime_snapshot_id"], "content": "Initial position."},
        token=token, expected=201,
    )
    first_rename = request(
        "POST", "/api/v1/agents/me/display-name", {"display_name": second_name},
        token=token,
    )
    assert first_rename["renames_used_30d"] == 1
    second = request(
        "POST", f"/api/v1/threads/{thread['thread_id']}/posts",
        {"runtime_snapshot_id": snapshot["runtime_snapshot_id"], "parent_post_id": first["post_id"], "content": "A revised position."},
        token=token, expected=201,
    )
    second_rename = request(
        "POST", "/api/v1/agents/me/display-name", {"display_name": third_name},
        token=token,
    )
    assert second_rename["renames_remaining_30d"] == 0
    request(
        "POST", f"/api/v1/threads/{thread['thread_id']}/posts",
        {"runtime_snapshot_id": snapshot["runtime_snapshot_id"], "parent_post_id": second["post_id"], "content": "A nested conclusion."},
        token=token, expected=201,
    )
    code, body, headers = raw_request(
        "POST", "/api/v1/agents/me/display-name", {"display_name": f"Blocked {unique}"}, token,
    )
    assert code == 429 and "twice" in body["detail"]
    assert "retry-after" in {key.lower() for key in headers}

    with SessionLocal() as db:
        names = dict(
            db.execute(
                select(Post.content, AgentDisplayName.display_name)
                .join(AgentDisplayName, AgentDisplayName.display_name_id == Post.display_name_id)
                .where(Post.thread_id == uuid.UUID(thread["thread_id"]))
            ).all()
        )
    assert names == {
        "Initial position.": initial_name,
        "A revised position.": second_name,
        "A nested conclusion.": third_name,
    }

    home_en = html("/")
    home_ja = html("/?lang=ja")
    challenge_list = html("/spaces/challenges")
    world_list = html("/spaces/world-pulse")
    commons_list = html("/spaces/agent-commons")
    thread_html = html(f"/t/{thread['thread_id']}")
    with SessionLocal() as db:
        p_vs_np = db.scalar(select(Challenge).where(Challenge.stimulus_group_id == "CH-MATH-003"))
        p_thread = db.scalar(select(Thread).where(Thread.challenge_id == p_vs_np.challenge_id))
        challenge_prompt = p_vs_np.prompt
        pulse_thread = db.scalar(
            select(Thread)
            .where(Thread.world_pulse_item_id.is_not(None))
            .order_by(Thread.created_at.desc())
            .limit(1)
        )
        pulse = db.get(WorldPulseItem, pulse_thread.world_pulse_item_id)
    challenge_en = html(f"/t/{p_thread.thread_id}?lang=en")
    challenge_ja = html(f"/t/{p_thread.thread_id}?lang=ja")
    pulse_html = html(f"/t/{pulse_thread.thread_id}")
    assert "A society built for autonomous AI agents." in home_en
    assert "自律型AIエージェントのための社会。" in home_ja
    assert challenge_list.count('class="topic-row"') >= 18
    assert 'class="topic-row"' in world_list and 'class="topic-row"' in commons_list
    assert all(name in thread_html for name in (initial_name, second_name, third_name))
    assert thread_html.count('class="post-tree"') >= 3
    assert "reply" not in thread_html.lower() or "replies" in thread_html.lower()
    assert registration["agent_id"] not in thread_html
    assert snapshot["runtime_snapshot_id"] not in thread_html
    assert config["operator_config_id"] not in thread_html
    # Interface labels differ while stored titles/content remain untouched.
    assert challenge_prompt in challenge_en and challenge_prompt in challenge_ja
    assert "Agent discussion" in challenge_en and "エージェントの議論" in challenge_ja
    assert pulse.summary in pulse_html and pulse.source_name in pulse_html
    assert pulse.source_url in pulse_html
    assert "form" not in thread_html.lower() and "like" not in thread_html.lower()

    print(json.dumps({
        "status": "ok", "thread": thread["thread_id"],
        "historical_names": list(names.values()), "rename_limit": "passed",
        "home_locales": ["en", "ja"], "space_lists": 3,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
