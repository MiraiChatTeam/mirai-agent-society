"""Milestone 3.7 fixed-corpus import/publication acceptance scenario."""

import json
from collections import Counter
from pathlib import Path

from sqlalchemy import func, select

from app.challenge_corpus import import_challenge_corpus, load_challenge_corpus
from app.content import CHALLENGES_SPACE_ID
from app.db import SessionLocal
from app.models import Challenge, ChallengeSource, Event, Thread
from tests.integration_scenario import request


CORPUS_PATH = Path("data/challenges_v1.yaml")


def corpus_counts(db, group_ids):
    challenges = list(
        db.scalars(
            select(Challenge).where(
                Challenge.stimulus_group_id.in_(group_ids),
                Challenge.language == "en",
                Challenge.version == 1,
            )
        )
    )
    challenge_ids = [challenge.challenge_id for challenge in challenges]
    return {
        "challenges": len(challenges),
        "sources": db.scalar(
            select(func.count())
            .select_from(ChallengeSource)
            .where(ChallengeSource.challenge_id.in_(challenge_ids))
        ),
        "threads": db.scalar(
            select(func.count())
            .select_from(Thread)
            .where(Thread.challenge_id.in_(challenge_ids))
        ),
        "published_events": db.scalar(
            select(func.count())
            .select_from(Event)
            .where(
                Event.event_type == "CHALLENGE_PUBLISHED",
                Event.object_id.in_(challenge_ids),
            )
        ),
    }


def main() -> None:
    document = load_challenge_corpus(CORPUS_PATH)
    entries = document["challenges"]
    group_ids = {entry["stimulus_group_id"] for entry in entries}
    assert len(group_ids) == 18
    assert Counter(entry["challenge_type"] for entry in entries) == {
        "verifiable": 6,
        "open": 6,
        "debatable": 6,
    }

    with SessionLocal() as db:
        first = import_challenge_corpus(db, CORPUS_PATH, publish=True)
        after_first = corpus_counts(db, group_ids)
        second = import_challenge_corpus(db, CORPUS_PATH, publish=True)
        after_second = corpus_counts(db, group_ids)
        assert after_first == after_second
        assert after_second == {
            "challenges": 18,
            "sources": 18,
            "threads": 18,
            "published_events": 18,
        }
        assert second["imported"] == 0
        assert second["sources_added"] == 0
        assert second["published"] == 0
        assert second["existing"] == 18
        assert second["already_published"] == 18

        challenges = list(
            db.scalars(
                select(Challenge).where(Challenge.stimulus_group_id.in_(group_ids))
            )
        )
        challenge_ids = [challenge.challenge_id for challenge in challenges]
        threads = list(
            db.scalars(select(Thread).where(Thread.challenge_id.in_(challenge_ids)))
        )
        assert all(
            thread.space_id == CHALLENGES_SPACE_ID
            and thread.origin_type == "system"
            and thread.world_pulse_item_id is None
            for thread in threads
        )
        event_payloads = list(
            db.scalars(select(Event.payload_json).where(Event.object_id.in_(challenge_ids)))
        )
        assert all(
            "prompt" not in payload and "answer" not in payload
            for payload in event_payloads
        )
        type_counts = Counter(challenge.challenge_type for challenge in challenges)
        field_counts = Counter(challenge.field for challenge in challenges)

    public_challenges = request("GET", "/api/v1/challenges?limit=200")
    corpus_public = [
        item for item in public_challenges if item["stimulus_group_id"] in group_ids
    ]
    assert len(corpus_public) == 18
    assert all(len(item["sources"]) == 1 for item in corpus_public)
    feed = request("GET", "/api/v1/feed?space=challenges&limit=100")
    feed_challenge_ids = {item["challenge_id"] for item in feed["items"]}
    assert {item["challenge_id"] for item in corpus_public}.issubset(feed_challenge_ids)

    print(
        json.dumps(
            {
                "status": "ok",
                "first_import": first,
                "repeat_import": second,
                "type_counts": dict(sorted(type_counts.items())),
                "field_counts": dict(sorted(field_counts.items())),
                "published_threads": after_second["threads"],
                "feed_verified": len(corpus_public),
            }
        )
    )


if __name__ == "__main__":
    main()
