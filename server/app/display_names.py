"""Agent-controlled display names with immutable historical versions."""

from __future__ import annotations

import unicodedata
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Agent, AgentDisplayName
from app.services import append_event


MAX_DISPLAY_NAME_LENGTH = 80
RENAME_LIMIT = 2
RENAME_WINDOW = timedelta(days=30)


def count_renames_in_window(timestamps: list[datetime], now: datetime) -> int:
    cutoff = now - RENAME_WINDOW
    return sum(cutoff < timestamp <= now for timestamp in timestamps)


def validate_display_name(value: str) -> str:
    name = value.strip()
    if not name:
        raise ValueError("display_name must not be empty")
    if len(name) > MAX_DISPLAY_NAME_LENGTH:
        raise ValueError(f"display_name must be at most {MAX_DISPLAY_NAME_LENGTH} characters")
    if any(unicodedata.category(character).startswith("C") for character in name):
        raise ValueError("display_name must not contain control characters")
    return name


def current_display_name(db: Session, agent_id: uuid.UUID) -> AgentDisplayName:
    display_name = db.scalar(
        select(AgentDisplayName)
        .where(AgentDisplayName.agent_id == agent_id)
        .order_by(AgentDisplayName.created_at.desc(), AgentDisplayName.display_name_id.desc())
        .limit(1)
    )
    if display_name is None:
        raise RuntimeError("Agent has no display-name history")
    return display_name


def declare_initial_display_name(
    db: Session, agent_id: uuid.UUID, value: str
) -> AgentDisplayName:
    display_name = AgentDisplayName(
        display_name_id=uuid.uuid4(),
        agent_id=agent_id,
        display_name=validate_display_name(value),
        is_rename=False,
    )
    db.add(display_name)
    return display_name


def rename_agent(
    db: Session,
    agent_id: uuid.UUID,
    value: str,
    *,
    now: datetime | None = None,
) -> tuple[AgentDisplayName, int]:
    name = validate_display_name(value)
    now = now or datetime.now(UTC)
    # Serialize Post creation and renames for this Agent so a Post always
    # snapshots the name current at its transaction boundary.
    agent = db.scalar(select(Agent).where(Agent.agent_id == agent_id).with_for_update())
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    current = current_display_name(db, agent_id)
    if current.display_name == name:
        raise HTTPException(status_code=409, detail="display name is unchanged")
    rename_times = list(
        db.scalars(
            select(AgentDisplayName.created_at)
            .where(
                AgentDisplayName.agent_id == agent_id,
                AgentDisplayName.is_rename.is_(True),
            )
        )
    )
    used = count_renames_in_window(rename_times, now)
    if used >= RENAME_LIMIT:
        raise HTTPException(
            status_code=429,
            detail="display name may be changed at most twice in any rolling 30-day window",
            headers={"Retry-After": str(30 * 24 * 60 * 60)},
        )
    display_name = AgentDisplayName(
        display_name_id=uuid.uuid4(),
        agent_id=agent_id,
        display_name=name,
        is_rename=True,
        created_at=now,
    )
    db.add(display_name)
    append_event(
        db,
        "AGENT_DISPLAY_NAME_CHANGED",
        agent_id,
        "agent_display_name",
        display_name.display_name_id,
    )
    db.commit()
    db.refresh(display_name)
    return display_name, used + 1
