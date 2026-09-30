"""Isolated API/DB proof for independent language provenance and one-time backfill."""

import base64
import uuid

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.admin import backfill_agent_onboarding_language
from app.db import SessionLocal
from app.models import Agent, AgentAdmission, Event, OperatorConfig, Post, RuntimeSnapshot
from tests.integration_scenario import (
    approved_config, authenticate, clear_rate_limits, new_invite, public_key_b64,
    request, snapshot_body,
)
from tests.scenario_guard import require_isolated_test_environment


def register(language=None, source="unknown"):
    _, invite = new_invite()
    key = Ed25519PrivateKey.generate()
    body = {
        "invite_token": invite, "public_key": public_key_b64(key),
        "display_name": f"Language Fixture {uuid.uuid4().hex[:10]}",
    }
    if language is not None:
        body.update(onboarding_language=language, onboarding_language_source=source)
    return request("POST", "/api/v1/agents", body, expected=201), key


def main() -> None:
    require_isolated_test_environment()
    clear_rate_limits()
    records = {}
    for language, source in (("zh", "agent_declared"), ("en", "operator_confirmed"),
                             ("ja", "agent_declared")):
        registration, key = register(language, source)
        assert (registration["onboarding_language"], registration["onboarding_language_source"]) == (language, source)
        records[language] = (registration, key)
        with SessionLocal() as db:
            agent = db.get(Agent, uuid.UUID(registration["agent_id"]))
            assert (agent.onboarding_language, agent.onboarding_language_source) == (language, source)

    legacy, legacy_key = register()
    legacy_id = uuid.UUID(legacy["agent_id"])
    assert (legacy["onboarding_language"], legacy["onboarding_language_source"]) == (None, "unknown")
    legacy_token, _ = authenticate(legacy, legacy_key)
    legacy_config = request("POST", "/api/v1/operator-configs",
        {"config_version": "0.4", "config_json": approved_config(5, 5)},
        legacy_token, expected=201)
    legacy_snapshot_body = snapshot_body(legacy_config["operator_config_id"])
    legacy_snapshot_body["locale"] = "en-US"
    legacy_snapshot = request("POST", "/api/v1/runtime-snapshots",
                              legacy_snapshot_body, legacy_token, expected=201)
    legacy_thread = request("POST", "/api/v1/threads",
                            {"title": "Legacy language fixture"}, legacy_token, expected=201)
    legacy_post = request("POST", f"/api/v1/threads/{legacy_thread['thread_id']}/posts", {
        "runtime_snapshot_id": legacy_snapshot["runtime_snapshot_id"],
        "content": "A fixture Post that cannot establish onboarding language.",
        "language": "en",
    }, legacy_token, expected=201)
    with SessionLocal() as db:
        assert db.get(Agent, legacy_id).onboarding_language is None
    registration, key = records["zh"]
    agent_id = uuid.UUID(registration["agent_id"])
    token, _ = authenticate(registration, key)
    config = request("POST", "/api/v1/operator-configs",
                     {"config_version": "0.4", "config_json": approved_config(5, 5)},
                     token, expected=201)
    snapshot_data = snapshot_body(config["operator_config_id"])
    snapshot_data["locale"] = "ja-JP"
    snapshot = request("POST", "/api/v1/runtime-snapshots", snapshot_data, token, expected=201)
    thread = request("POST", "/api/v1/threads", {"title": "Language provenance fixture"}, token, expected=201)
    post = request("POST", f"/api/v1/threads/{thread['thread_id']}/posts", {
        "runtime_snapshot_id": snapshot["runtime_snapshot_id"], "content": "An English fixture.",
        "language": "en",
    }, token, expected=201)
    assert (post["language"], post["language_source"]) == ("en", "declared")
    with SessionLocal() as db:
        agent = db.get(Agent, agent_id)
        runtime = db.get(RuntimeSnapshot, uuid.UUID(snapshot["runtime_snapshot_id"]))
        public_post = db.get(Post, uuid.UUID(post["post_id"]))
        assert (agent.onboarding_language, runtime.locale, public_post.language) == ("zh", "ja-JP", "en")
        assert public_post.language_source == "declared"
    request("POST", "/api/v1/agents/me/display-name",
            {"display_name": f"Renamed Language Fixture {uuid.uuid4().hex[:8]}"}, token)
    challenge = request("POST", "/api/v1/auth/recovery/challenge",
                        {"public_key": public_key_b64(key)}, expected=201)
    signature = base64.b64encode(key.sign(challenge["signed_message"].encode())).decode()
    recovered = request("POST", "/api/v1/auth/recovery/verify",
                        {"challenge_id": challenge["challenge_id"], "signature": signature})
    assert recovered["agent_id"] == registration["agent_id"]
    with SessionLocal() as db:
        assert db.get(Agent, agent_id).onboarding_language == "zh"

    with SessionLocal() as db:
        admission = db.get(AgentAdmission, legacy_id)
        before = (admission.invite_id, admission.admission_mode, admission.admission_cohort, admission.registered_at)
        assert db.get(Agent, legacy_id).onboarding_language is None
        result = backfill_agent_onboarding_language(
            db, legacy_id, language="ja", source="operator_confirmed",
            evidence_reference="confirmed historical onboarding transcript 2026-09-29",
        )
        assert result["onboarding_language"] == "ja"
        assert "evidence_reference" not in result
    with SessionLocal() as db:
        agent = db.get(Agent, legacy_id)
        admission = db.get(AgentAdmission, legacy_id)
        assert (agent.onboarding_language, agent.onboarding_language_source) == ("ja", "operator_confirmed")
        assert (admission.invite_id, admission.admission_mode, admission.admission_cohort, admission.registered_at) == before
        stored_config = db.get(OperatorConfig, uuid.UUID(legacy_config["operator_config_id"]))
        stored_runtime = db.get(RuntimeSnapshot, uuid.UUID(legacy_snapshot["runtime_snapshot_id"]))
        stored_post = db.get(Post, uuid.UUID(legacy_post["post_id"]))
        assert stored_config.agent_id == legacy_id and stored_config.config_json == approved_config(5, 5)
        assert stored_runtime.agent_id == legacy_id and stored_runtime.locale == "en-US"
        assert stored_post.author_agent_id == legacy_id and stored_post.language == "en"
        audit = db.scalar(select(Event).where(Event.object_id == legacy_id,
            Event.event_type == "AGENT_ONBOARDING_LANGUAGE_BACKFILLED"))
        assert audit is not None and audit.actor_agent_id is None
        assert "evidence_reference" not in audit.payload_json
        assert "evidence_reference_sha256" in audit.payload_json
        try:
            backfill_agent_onboarding_language(
                db, legacy_id, language="en", source="operator_confirmed", evidence_reference="other"
            )
        except ValueError:
            pass
        else:
            raise AssertionError("established provenance was overwritten")
    with SessionLocal() as db:
        try:
            db.execute(text("UPDATE agents SET onboarding_language='en' WHERE agent_id=:id"), {"id": legacy_id})
            db.commit()
        except DBAPIError:
            db.rollback()
        else:
            raise AssertionError("DB immutability trigger did not reject update")
    clear_rate_limits()
    print("Isolated language provenance API, recovery, rename and backfill passed")


if __name__ == "__main__":
    main()
