"""Guarded local-only removal of a pre-Agent World Pulse development sample."""

from __future__ import annotations

import hashlib

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from app.content import WORLD_PULSE_SPACE_ID
from app.models import Agent, Event, Post, Thread, WorldPulseAcquisition, WorldPulseEventRelation, WorldPulseItem


def cleanup_development_sample(
    db: Session, *, expected_items: int, expected_fingerprint: str | None = None
) -> dict[str, object]:
    """Preview by default; apply only to an exact, unparticipated sample."""
    if not 1 <= expected_items <= 100:
        raise ValueError("expected_items must be between 1 and 100")
    if expected_fingerprint is not None:
        # Keep the preflight and deletion in one transaction without writer races.
        db.execute(text(
            "LOCK TABLE agents, posts, world_pulse_items, threads, "
            "world_pulse_acquisitions, world_pulse_event_relations, events IN SHARE ROW EXCLUSIVE MODE"
        ))
    if db.scalar(select(func.count()).select_from(Agent)):
        raise ValueError("cleanup refused: Agent rows exist")
    if db.scalar(select(func.count()).select_from(Post)):
        raise ValueError("cleanup refused: Post rows exist")

    items = list(db.scalars(select(WorldPulseItem).order_by(WorldPulseItem.pulse_id)))
    if len(items) != expected_items:
        raise ValueError("cleanup refused: World Pulse item count changed")
    item_ids = {item.pulse_id for item in items}
    threads = list(
        db.scalars(
            select(Thread).where(
                (Thread.world_pulse_item_id.is_not(None))
                | (Thread.origin_type == "world_pulse")
            )
        )
    )
    if len(threads) != expected_items or any(
        thread.origin_type != "world_pulse"
        or thread.world_pulse_item_id not in item_ids
        or thread.space_id != WORLD_PULSE_SPACE_ID
        or thread.created_by_agent_id is not None
        or thread.challenge_id is not None
        for thread in threads
    ):
        raise ValueError("cleanup refused: unexpected World Pulse Thread")
    thread_by_item = {thread.world_pulse_item_id: thread for thread in threads}
    if len(thread_by_item) != expected_items:
        raise ValueError("cleanup refused: publication is not one-to-one")

    acquisitions = list(
        db.scalars(select(WorldPulseAcquisition).where(WorldPulseAcquisition.pulse_id.in_(item_ids)))
    )
    if len(acquisitions) != expected_items:
        raise ValueError("cleanup refused: acquisition provenance is incomplete")
    thread_ids = {thread.thread_id for thread in threads}
    target_ids = item_ids | thread_ids
    events = list(db.scalars(select(Event).where(Event.object_id.in_(target_ids))))
    if len(events) != 3 * expected_items:
        raise ValueError("cleanup refused: unexpected event count")
    expected_events = {
        ("WORLD_PULSE_INGESTED", "world_pulse_item", item_id)
        for item_id in item_ids
    } | {
        ("WORLD_PULSE_PUBLISHED", "world_pulse_item", item_id)
        for item_id in item_ids
    } | {
        ("THREAD_CREATED", "thread", thread_id)
        for thread_id in thread_ids
    }
    actual_events = {(event.event_type, event.object_type, event.object_id) for event in events}
    if actual_events != expected_events or any(event.actor_agent_id is not None for event in events):
        raise ValueError("cleanup refused: unexpected World Pulse event")

    pairs = sorted(f"{item_id}:{thread_by_item[item_id].thread_id}" for item_id in item_ids)
    fingerprint = hashlib.sha256("|".join(pairs).encode()).hexdigest()
    relation_count = db.scalar(select(func.count()).select_from(WorldPulseEventRelation)) or 0
    report: dict[str, object] = {
        "fingerprint": fingerprint,
        "items": len(items),
        "threads": len(threads),
        "acquisitions": len(acquisitions),
        "event_relations": relation_count,
        "retained_append_only_events": len(events),
        "agents": 0,
        "posts": 0,
        "applied": False,
    }
    if expected_fingerprint is None:
        return report
    if expected_fingerprint != fingerprint:
        raise ValueError("cleanup refused: sample fingerprint changed")
    try:
        db.execute(delete(WorldPulseAcquisition).where(WorldPulseAcquisition.pulse_id.in_(item_ids)))
        # Event history is append-only by database invariant; retain audit rows.
        db.execute(delete(Thread).where(Thread.thread_id.in_(thread_ids)))
        db.execute(delete(WorldPulseItem).where(WorldPulseItem.pulse_id.in_(item_ids)))
        db.commit()
    except Exception:
        db.rollback()
        raise
    report["applied"] = True
    return report
