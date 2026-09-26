"""Offline Milestone 3.6 acquisition acceptance scenario."""

import json
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import Event, Thread, WorldPulseAcquisition, WorldPulseItem
from app.world_pulse_acquisition import run_pipeline
from app.world_pulse_collectors import RSSCollector
from tests.integration_scenario import request
from tests.scenario_guard import require_isolated_test_environment


FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 9, 22, 2, 0, tzinfo=UTC)


class MappingFixtureClient:
    def __init__(self, token):
        self.token = token

    def get(self, url, *, allowed_hosts, timeout=10, max_bytes=1_000_000):
        payload = (FIXTURES / url.rsplit("/", 1)[-1]).read_text()
        for host in ("global", "jp", "zh"):
            payload = payload.replace(
                f"https://{host}.example", f"https://{host}-{self.token}.example"
            )
        return payload.encode()


class NoPublisherMetadata:
    def fetch(self, source_url, *, aggregator=False):
        return None


class FailingCollector:
    adapter_id = "fixture-failing"
    profile = "global-en"

    def collect(self, client):
        raise TimeoutError("intentional offline fixture failure")


def collectors():
    return [
        RSSCollector(
            "fixture-global", "global-en", "https://fixture.test/global_en.xml",
            "news", "Global Fixture", "en", frozenset({"fixture.test"}),
        ),
        RSSCollector(
            "fixture-japan", "japan-ja", "https://fixture.test/japan_ja.xml",
            "news", "Japan Fixture", "ja", frozenset({"fixture.test"}),
        ),
        RSSCollector(
            "fixture-china", "china-zh", "https://fixture.test/china_zh.xml",
            "news", "Chinese Fixture", "zh", frozenset({"fixture.test"}),
        ),
        FailingCollector(),
    ]


def counts(db):
    return {
        "items": db.scalar(select(func.count()).select_from(WorldPulseItem)),
        "threads": db.scalar(select(func.count()).select_from(Thread)),
        "events": db.scalar(select(func.count()).select_from(Event)),
        "acquisitions": db.scalar(select(func.count()).select_from(WorldPulseAcquisition)),
    }


def main() -> None:
    require_isolated_test_environment()
    run_token = uuid.uuid4().hex
    fixture_collectors = collectors()
    fixture_client = MappingFixtureClient(run_token[:12])
    # Make stored URLs unique across repeated executions while preserving identical
    # inputs within this scenario run.
    for index, collector in enumerate(fixture_collectors):
        if isinstance(collector, RSSCollector):
            object.__setattr__(
                collector,
                "adapter_id",
                f"{collector.adapter_id}-{run_token[:8]}-{index}",
            )

    with SessionLocal() as db:
        latest_date = db.scalar(select(func.max(WorldPulseAcquisition.selection_date)))
        selection_date = max(latest_date + timedelta(days=1), date(2090, 1, 1)) if latest_date else date(2090, 1, 1)
        before = counts(db)
        dry = run_pipeline(
            db,
            fixture_collectors,
            dry_run=True,
            now=NOW,
            selection_date=selection_date,
            client=fixture_client, publisher_client=NoPublisherMetadata(),
        )
        after_dry = counts(db)
        assert after_dry == before
        assert dry["sources_attempted"] == 4
        assert dry["sources_succeeded"] == 3
        assert dry["duplicates_removed"] == 1
        assert sum(source["normalized_count"] for source in dry["source_results"]) == dry["candidates"] - dry["malformed_rejected"]
        assert sum(source["selected_count"] for source in dry["source_results"]) == dry["selected"]
        assert all(source["published_count"] == 0 for source in dry["source_results"])
        assert len(dry["errors"]) == 1
        assert 0 < dry["selected"] <= 10
        lunar = [item for item in dry["items"] if item["title"] == "Shared lunar mission launches successfully"]
        assert len(lunar) == 1
        assert dry["cross_source_events_suppressed"] >= 1

        real = run_pipeline(
            db,
            fixture_collectors,
            dry_run=False,
            now=NOW,
            selection_date=selection_date,
            client=fixture_client, publisher_client=NoPublisherMetadata(),
        )
        assert real["selected"] == dry["selected"]
        assert real["ingested"] == dry["selected"]
        assert real["published"] == dry["selected"]
        selected_urls = {item["source_url"] for item in real["items"]}
        stored = list(
            db.scalars(
                select(WorldPulseItem).where(WorldPulseItem.source_url.in_(selected_urls))
            )
        )
        pulse_ids = [item.pulse_id for item in stored]
        assert len(stored) == real["selected"]
        assert all(item.summary == "Discuss this development." for item in stored)
        assert all(item.stimulus_summary is None or len(item.stimulus_summary) <= 800 for item in stored)
        assert any(item.summary_source == "feed_metadata" for item in stored)
        assert all(item.summary_source != "publisher_page" for item in stored)
        assert all("Mission controllers confirmed a successful launch." not in (item.stimulus_summary or "") for item in stored)
        assert sum(source["published_count"] for source in real["source_results"]) == real["published"]
        threads = list(
            db.scalars(
                select(Thread).where(Thread.world_pulse_item_id.in_(pulse_ids))
            )
        )
        assert len(threads) == real["selected"]
        assert all(thread.origin_type == "world_pulse" for thread in threads)
        publication_events = db.scalar(
            select(func.count())
            .select_from(Event)
            .where(
                Event.event_type == "WORLD_PULSE_PUBLISHED",
                Event.object_id.in_(pulse_ids),
            )
        )
        event_payloads = list(
            db.scalars(select(Event.payload_json).where(Event.object_id.in_(pulse_ids)))
        )
        assert all("summary" not in payload and "body" not in payload for payload in event_payloads)
        after_real = counts(db)

        # Simulate a pre-0009 item and verify dry-run is read-only, then backfill.
        missing_context = next(item for item in stored if item.summary_source == "feed_metadata")
        missing_context.stimulus_summary = None
        missing_context.summary_source = "unavailable"
        db.commit()
        preview_backfill = run_pipeline(
            db, fixture_collectors, dry_run=True, now=NOW,
            selection_date=selection_date, client=fixture_client, publisher_client=NoPublisherMetadata(),
        )
        db.refresh(missing_context)
        assert preview_backfill["backfill_available"] == 1
        assert missing_context.stimulus_summary is None
        backfill = run_pipeline(
            db, fixture_collectors, dry_run=False, now=NOW,
            selection_date=selection_date, client=fixture_client, publisher_client=NoPublisherMetadata(),
        )
        db.refresh(missing_context)
        assert backfill["backfilled"] == 1
        assert backfill["ingested"] == backfill["published"] == 0
        assert missing_context.summary_source == "feed_metadata"
        assert counts(db) == after_real

        repeated = run_pipeline(
            db,
            fixture_collectors,
            dry_run=False,
            now=NOW,
            selection_date=selection_date,
            client=fixture_client, publisher_client=NoPublisherMetadata(),
        )
        assert repeated["ingested"] == repeated["published"] == repeated["backfilled"] == 0
        assert counts(db) == after_real
        assert db.scalar(
            select(func.count())
            .select_from(Event)
            .where(
                Event.event_type == "WORLD_PULSE_PUBLISHED",
                Event.object_id.in_(pulse_ids),
            )
        ) == publication_events

    feed = request("GET", "/api/v1/feed?space=world-pulse&limit=100")
    feed_ids = {item["world_pulse_item_id"] for item in feed["items"]}
    assert {str(pulse_id) for pulse_id in pulse_ids}.issubset(feed_ids)
    print(
        json.dumps(
            {
                "status": "ok",
                "dry_run": dry,
                "real_run": real,
                "backfill_run": backfill,
                "repeat_run": repeated,
                "feed_verified": len(pulse_ids),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
