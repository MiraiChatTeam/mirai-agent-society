"""Conservative, source-preserving relations between published NEWS items."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import WorldPulseEventRelation, WorldPulseItem


MAX_SAME_EVENT_GAP = timedelta(hours=24)
ENTITY_STOPWORDS = frozenset({
    "A", "An", "And", "After", "As", "At", "Breaking", "For", "From",
    "In", "Live", "News", "Of", "On", "The", "To", "Update", "Updates",
    "World", "Says", "Report", "Reports", "President", "Government",
})
ACTION_PATTERNS = {
    "airport_seized": re.compile(r"\b(?:seize|seized|take|took|capture|captured)\b.*\bairports?\b|\bairports?\b.*\b(?:seize|seized|take|took|capture|captured)\b", re.I),
    "offensive_launched": re.compile(r"\b(?:launch|launched|begin|began|start|started)\b.*\boffensives?\b|\boffensives?\b.*\b(?:launch|launched|begin|began|start|started)\b", re.I),
    "talks_held": re.compile(r"\b(?:hold|held|meet|met|meeting|talks|negotiations)\b", re.I),
    "speech_delivered": re.compile(r"\b(?:speech|address)\b.*\b(?:deliver|delivered|give|gave|make|made)\b|\b(?:deliver|delivered|give|gave|make|made)\b.*\b(?:speech|address)\b", re.I),
    "eviction": re.compile(r"\b(?:evict|evicted|eviction)\b", re.I),
}
CONTENT_STOPWORDS = frozenset({
    "about", "after", "amid", "and", "are", "for", "from", "has", "have",
    "into", "its", "new", "the", "this", "with", "world", "news",
})


def _entities(title: str) -> frozenset[str]:
    """Use explicit capitalized names only; this deliberately misses some events."""
    return frozenset(
        token.casefold() for token in re.findall(r"\b[A-Z][A-Za-z]{2,}\b", title)
        if token not in ENTITY_STOPWORDS
    )


def _action(title: str) -> str | None:
    matches = [name for name, pattern in ACTION_PATTERNS.items() if pattern.search(title)]
    return matches[0] if len(matches) == 1 else None


def _tokens(title: str) -> frozenset[str]:
    return frozenset(
        token for token in re.findall(r"[a-z]{3,}", title.casefold())
        if token not in CONTENT_STOPWORDS
    )


def same_event(left: WorldPulseItem, right: WorldPulseItem) -> bool:
    """Assert only close, entity/action-aligned English cross-source reports."""
    if left.pulse_id == right.pulse_id or left.source_type != "news" or right.source_type != "news":
        return False
    if left.language != "en" or right.language != "en" or left.source_name == right.source_name:
        return False
    if abs(left.published_at - right.published_at) > MAX_SAME_EVENT_GAP:
        return False
    action = _action(left.title)
    if action is None or action != _action(right.title):
        return False
    if len(_entities(left.title) & _entities(right.title)) < 2:
        return False
    first, second = _tokens(left.title), _tokens(right.title)
    common = first & second
    return len(common) >= 3 and len(common) / len(first | second) >= 0.25


def record_event_relations(db: Session, *, since: datetime) -> list[tuple[str, str]]:
    """Add missing pair relations; never replace items or append-only Events."""
    items = list(db.scalars(
        select(WorldPulseItem)
        .where(WorldPulseItem.published_at >= since, WorldPulseItem.source_type == "news")
        .order_by(WorldPulseItem.published_at, WorldPulseItem.pulse_id)
    ))
    if not items:
        return []
    item_ids = [item.pulse_id for item in items]
    existing = {
        (row.left_pulse_id, row.right_pulse_id)
        for row in db.scalars(
            select(WorldPulseEventRelation).where(
                WorldPulseEventRelation.left_pulse_id.in_(item_ids)
            )
        )
    }
    created: list[tuple[str, str]] = []
    for index, left in enumerate(items):
        for right in items[index + 1:]:
            if right.published_at - left.published_at > MAX_SAME_EVENT_GAP:
                break
            if not same_event(left, right):
                continue
            first, second = sorted((left.pulse_id, right.pulse_id))
            if (first, second) in existing:
                continue
            db.add(WorldPulseEventRelation(
                left_pulse_id=first, right_pulse_id=second,
                relation_type="same_event", evidence_code="entity_action_time_v1",
            ))
            existing.add((first, second))
            created.append((str(first), str(second)))
    if created:
        db.commit()
    return created
