import uuid
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Event


def not_found(resource: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"{resource} not found")


def append_event(
    db: Session,
    event_type: str,
    actor_agent_id: uuid.UUID | None,
    object_type: str,
    object_id: uuid.UUID,
    payload: dict[str, object] | None = None,
) -> None:
    db.add(
        Event(
            event_id=uuid.uuid4(),
            created_at=datetime.now(UTC),
            event_type=event_type,
            actor_agent_id=actor_agent_id,
            object_type=object_type,
            object_id=object_id,
            payload_json=payload or {},
        )
    )


def commit_creation(db: Session, entity: object) -> object:
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="database constraint conflict") from exc
    db.refresh(entity)
    return entity
