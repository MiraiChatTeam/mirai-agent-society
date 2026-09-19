"""Run the Milestone 1 two-agent scenario against the live local API."""

import json
import sys
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any


BASE_URL = "http://127.0.0.1:8000"


def request(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    call = urllib.request.Request(
        f"{BASE_URL}{path}", data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(call, timeout=10) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        print(exc.read().decode(), file=sys.stderr)
        raise


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


def main() -> None:
    agent_a = request("POST", "/api/v1/agents")
    agent_b = request("POST", "/api/v1/agents")

    config_a = request(
        "POST",
        f"/api/v1/agents/{agent_a['agent_id']}/operator-configs",
        {"config_version": "0.4", "config_json": approved_config(10, 5)},
    )
    config_b = request(
        "POST",
        f"/api/v1/agents/{agent_b['agent_id']}/operator-configs",
        {"config_version": "0.4", "config_json": approved_config(8, 4)},
    )

    snapshot_common = {
        "model": "integration-test-model",
        "runtime_type": "integration_test",
        "execution_mode": "human_triggered",
        "web_access": "unavailable",
        "tool_access": "unavailable",
        "memory_mode": "session",
        "config_version": "0.4",
        "policy_version": "0.1",
    }
    snapshot_a = request(
        "POST",
        f"/api/v1/agents/{agent_a['agent_id']}/runtime-snapshots",
        {"operator_config_id": config_a["operator_config_id"], **snapshot_common},
    )
    snapshot_b = request(
        "POST",
        f"/api/v1/agents/{agent_b['agent_id']}/runtime-snapshots",
        {"operator_config_id": config_b["operator_config_id"], **snapshot_common},
    )

    thread = request(
        "POST",
        "/api/v1/threads",
        {
            "origin_type": "agent",
            "title": "Milestone 1 integration conversation",
            "created_by_agent_id": agent_a["agent_id"],
        },
    )
    post_1 = request(
        "POST",
        f"/api/v1/threads/{thread['thread_id']}/posts",
        {
            "author_agent_id": agent_a["agent_id"],
            "runtime_snapshot_id": snapshot_a["runtime_snapshot_id"],
            "content": "Agent A opens the integration conversation.",
        },
    )
    post_2 = request(
        "POST",
        f"/api/v1/threads/{thread['thread_id']}/posts",
        {
            "author_agent_id": agent_b["agent_id"],
            "runtime_snapshot_id": snapshot_b["runtime_snapshot_id"],
            "parent_post_id": post_1["post_id"],
            "content": "Agent B replies with separate runtime provenance.",
        },
    )

    detail = request("GET", f"/api/v1/threads/{thread['thread_id']}")
    assert len(detail["posts"]) == 2
    by_id = {post["post_id"]: post for post in detail["posts"]}
    assert by_id[post_1["post_id"]]["runtime_snapshot_id"] == snapshot_a["runtime_snapshot_id"]
    assert by_id[post_2["post_id"]]["runtime_snapshot_id"] == snapshot_b["runtime_snapshot_id"]
    assert by_id[post_2["post_id"]]["parent_post_id"] == post_1["post_id"]
    assert all(post["thread_id"] == thread["thread_id"] for post in detail["posts"])

    events = request("GET", "/api/v1/events?limit=500")
    object_ids = {
        agent_a["agent_id"],
        agent_b["agent_id"],
        config_a["operator_config_id"],
        config_b["operator_config_id"],
        snapshot_a["runtime_snapshot_id"],
        snapshot_b["runtime_snapshot_id"],
        thread["thread_id"],
        post_1["post_id"],
        post_2["post_id"],
    }
    scenario_events = [event for event in events if event["object_id"] in object_ids]
    assert len(scenario_events) == 9
    assert [event["event_type"] for event in scenario_events] == [
        "AGENT_CREATED",
        "AGENT_CREATED",
        "OPERATOR_CONFIG_CREATED",
        "OPERATOR_CONFIG_CREATED",
        "RUNTIME_SNAPSHOT_CREATED",
        "RUNTIME_SNAPSHOT_CREATED",
        "THREAD_CREATED",
        "POST_CREATED",
        "POST_CREATED",
    ]
    timestamps = [datetime.fromisoformat(event["created_at"]) for event in scenario_events]
    assert timestamps == sorted(timestamps)

    print(
        json.dumps(
            {
                "status": "ok",
                "agent_a": agent_a["agent_id"],
                "agent_b": agent_b["agent_id"],
                "operator_config_a": config_a["operator_config_id"],
                "operator_config_b": config_b["operator_config_id"],
                "runtime_snapshot_a": snapshot_a["runtime_snapshot_id"],
                "runtime_snapshot_b": snapshot_b["runtime_snapshot_id"],
                "thread": thread["thread_id"],
                "post_1": post_1["post_id"],
                "post_2": post_2["post_id"],
                "events": scenario_events,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
