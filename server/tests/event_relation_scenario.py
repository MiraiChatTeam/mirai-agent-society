"""Exercise durable, idempotent event relations without touching Event audit rows."""

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.content import ingest_world_pulse, publish_world_pulse
from app.db import SessionLocal
from app.models import Event, WorldPulseEventRelation, WorldPulseItem
from app.world_pulse_event_relations import record_event_relations
from tests.scenario_guard import require_isolated_test_environment


def main():
    require_isolated_test_environment()
    now = datetime.now(UTC)
    with SessionLocal() as db:
        ids = []
        for title, source, suffix in (
            ("Tigray forces seize airport in northern Ethiopia", "BBC fixture", "bbc"),
            ("Ethiopia: Tigray fighters capture airport in northern region",
             "Google News fixture", "google"),
        ):
            pulse = ingest_world_pulse(
                db, title=title, summary="Discuss this development.",
                language="en", published_at=now, source_type="news",
                source_url=f"https://example.test/event-relation/{suffix}",
                source_name=source,
            )
            publish_world_pulse(db, pulse.pulse_id)
            ids.append(pulse.pulse_id)
        audit_count = db.scalar(select(func.count()).select_from(Event))
        created = record_event_relations(db, since=now - timedelta(days=1))
        first, second = sorted(ids)
        relation = db.get(WorldPulseEventRelation, (first, second))
        assert relation is not None and relation.relation_type == "same_event"
        assert (str(first), str(second)) in created
        assert record_event_relations(db, since=now - timedelta(days=1)) == []
        assert db.scalar(select(func.count()).select_from(Event)) == audit_count
        assert all(db.get(WorldPulseItem, item_id) is not None for item_id in ids)
    print(json.dumps({"status": "ok", "relation": [str(first), str(second)],
                      "rerun_created": 0, "audit_events_unchanged": True}))


if __name__ == "__main__":
    main()
