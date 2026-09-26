"""Exercise guarded World Pulse sample cleanup in an isolated pre-Agent DB."""

import json
import uuid
from datetime import UTC, date, datetime

from sqlalchemy import func, select

from app.content import ingest_world_pulse, publish_world_pulse
from app.db import SessionLocal
from app.models import Agent, Challenge, Event, Post, Space, Thread, WorldPulseAcquisition, WorldPulseEventRelation, WorldPulseItem
from app.world_pulse_cleanup import cleanup_development_sample
from tests.scenario_guard import require_isolated_test_environment


def count(db, model):
    return db.scalar(select(func.count()).select_from(model))


def main():
    require_isolated_test_environment()
    with SessionLocal() as db:
        assert count(db, Agent) == count(db, Post) == count(db, WorldPulseItem) == 0
        preserved = {
            model.__tablename__: count(db, model)
            for model in (Challenge, Space, Thread, Event)
        }
        item_ids = []
        for index in range(2):
            item = ingest_world_pulse(
                db, title=f"Disposable feed item {index}",
                summary="Discuss this development.", language="en",
                published_at=datetime(2026, 9, 23, tzinfo=UTC),
                source_type="news",
                source_url=f"https://example.test/dev-sample/{uuid.uuid4()}",
                source_name="Test public feed",
            )
            publish_world_pulse(db, item.pulse_id)
            item_ids.append(item.pulse_id)
            db.add(WorldPulseAcquisition(
                acquisition_id=uuid.uuid4(), pulse_id=item.pulse_id,
                source_adapter="fixture-news", source_profile="global-en",
                acquired_at=datetime.now(UTC), selection_date=date.today(),
                source_rank=index + 1, selection_score=50,
                score_components={"source_rank": 50}, collector_version="test",
            ))
            db.commit()
        first, second = sorted(item_ids)
        db.add(WorldPulseEventRelation(
            left_pulse_id=first, right_pulse_id=second,
            relation_type="same_event", evidence_code="entity_action_time_v1",
        ))
        db.commit()
        preview = cleanup_development_sample(db, expected_items=2)
        assert preview["applied"] is False
        assert (preview["items"], preview["threads"], preview["acquisitions"],
                preview["retained_append_only_events"]) == (2, 2, 2, 6)
        assert count(db, WorldPulseItem) == 2
        try:
            cleanup_development_sample(
                db, expected_items=2, expected_fingerprint="wrong"
            )
            raise AssertionError("mismatched cleanup fingerprint was accepted")
        except ValueError:
            pass
        assert count(db, WorldPulseItem) == 2
        applied = cleanup_development_sample(
            db, expected_items=2, expected_fingerprint=preview["fingerprint"]
        )
        assert applied["applied"] is True
        assert count(db, WorldPulseItem) == count(db, WorldPulseAcquisition) == 0
        assert count(db, WorldPulseEventRelation) == 0
        for model in (Challenge, Space, Thread):
            assert count(db, model) == preserved[model.__tablename__]
        assert count(db, Event) == preserved[Event.__tablename__] + 6
        assert count(db, Agent) == count(db, Post) == 0
    print(json.dumps({"status": "ok", "preview": preview, "applied": applied}))


if __name__ == "__main__":
    main()
