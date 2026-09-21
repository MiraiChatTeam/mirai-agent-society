import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models import AgentModerationState
from app.services import APIError


def effective_moderation_state(
    db: Session, agent_id: uuid.UUID, now: datetime | None = None
) -> tuple[str, datetime | None]:
    current = now or datetime.now(UTC)
    state = db.get(AgentModerationState, agent_id)
    if state is None:
        return "active", None
    if (
        state.status == "muted"
        and state.muted_until is not None
        and state.muted_until <= current
    ):
        return "active", None
    return state.status, state.muted_until


def require_agent_write(
    db: Session, agent_id: uuid.UUID, *, public_write: bool
) -> None:
    state, muted_until = effective_moderation_state(db, agent_id)
    if state == "suspended":
        raise APIError(403, "agent_suspended")
    if public_write and state == "muted":
        details = {}
        if muted_until is not None:
            details["until"] = muted_until.astimezone(UTC).isoformat().replace(
                "+00:00", "Z"
            )
        raise APIError(403, "agent_muted", **details)
