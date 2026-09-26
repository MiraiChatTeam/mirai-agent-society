"""Isolated acceptance check for the frozen 50-Challenge publication path."""

import json
from collections import Counter
from pathlib import Path

from sqlalchemy import select

from app.challenge_corpus import import_challenge_corpus, load_challenge_corpus
from app.content import CHALLENGES_SPACE_ID
from app.db import SessionLocal
from app.models import Challenge, ChallengeSource, Event, Thread
from tests.scenario_guard import require_isolated_test_environment


SEED = Path("data/challenges_v1.yaml")
CORPUS = Path("data/challenges_v2.yaml")


def snapshot(db, group_ids):
    challenges = list(db.scalars(select(Challenge).where(Challenge.stimulus_group_id.in_(group_ids))))
    ids = {item.challenge_id for item in challenges}
    sources = list(db.scalars(select(ChallengeSource).where(ChallengeSource.challenge_id.in_(ids))))
    threads = list(db.scalars(select(Thread).where(Thread.challenge_id.in_(ids))))
    events = list(db.scalars(select(Event).where(
        Event.event_type == "CHALLENGE_PUBLISHED", Event.object_id.in_(ids)
    )))
    pairs = {item.stimulus_group_id: (
        str(item.challenge_id),
        str(next(thread.thread_id for thread in threads if thread.challenge_id == item.challenge_id)),
    ) for item in challenges}
    return challenges, sources, threads, events, pairs


def main() -> None:
    require_isolated_test_environment()
    seed_ids = {e["stimulus_group_id"] for e in load_challenge_corpus(SEED)["challenges"]}
    group_ids = {e["stimulus_group_id"] for e in load_challenge_corpus(CORPUS)["challenges"]}
    assert len(seed_ids) == 18 and len(group_ids) == 50 and seed_ids <= group_ids

    with SessionLocal() as db:
        import_challenge_corpus(db, SEED, publish=True)
        seed_pairs = snapshot(db, seed_ids)[4]
        first = import_challenge_corpus(db, CORPUS, publish=True)
        after_first = snapshot(db, group_ids)
        repeat = import_challenge_corpus(db, CORPUS, publish=True)
        after_repeat = snapshot(db, group_ids)
        assert after_first[4] == after_repeat[4]
        challenges, sources, threads, events, pairs = after_repeat
        assert len(challenges) == 50
        assert len(sources) == 81
        assert len(threads) == 50
        assert len(events) == 50
        assert {source.challenge_id for source in sources} == {item.challenge_id for item in challenges}
        assert {thread.challenge_id for thread in threads} == {item.challenge_id for item in challenges}
        assert all(pairs[key] == value for key, value in seed_pairs.items())
        assert len({(c.stimulus_group_id, c.language, c.version) for c in challenges}) == 50
        assert all(count == 1 for count in Counter(t.challenge_id for t in threads).values())
        assert all(
            t.origin_type == "system" and t.space_id == CHALLENGES_SPACE_ID
            and t.created_by_agent_id is None and t.world_pulse_item_id is None
            for t in threads
        )
        assert repeat["imported"] == repeat["sources_added"] == repeat["published"] == 0
        assert repeat["existing"] == repeat["already_published"] == 50
        print(json.dumps({
            "status": "ok",
            "first_import": {key: value for key, value in first.items() if key != "challenge_ids"},
            "repeat_import": {key: value for key, value in repeat.items() if key != "challenge_ids"},
            "challenges": len(challenges), "sources": len(sources),
            "threads": len(threads), "preserved_seed_pairs": len(seed_pairs),
        }))


if __name__ == "__main__":
    main()
