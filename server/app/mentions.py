"""Resolve visible Agent mentions once, at Post creation, to immutable UUIDs."""

import re
import uuid

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.continuity_models import AgentNameReservation
from app.display_names import normalized_name_key, validate_display_name


# Simple names use @Name; names with spaces/punctuation use @{Exact Name}.
# An @ inside an email address is not a mention.
MENTION_PATTERN = re.compile(r"(?<![\w@])@(?:\{([^{}\n]*)\}|([\w](?:[\w.-]*[\w])?))", re.UNICODE)
MAX_MENTIONS_PER_POST = 20


def resolve_mentions(db: Session, content: str) -> list[tuple[uuid.UUID, str]]:
    resolved: dict[uuid.UUID, str] = {}
    for marker in re.finditer(r"(?<![\w@])@\{", content):
        if MENTION_PATTERN.match(content, marker.start()) is None:
            raise HTTPException(status_code=422, detail="malformed Agent mention")
    for match in MENTION_PATTERN.finditer(content):
        raw_name = match.group(1) if match.group(1) is not None else match.group(2)
        try:
            name_used = validate_display_name(raw_name)
            name_key = normalized_name_key(name_used)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"invalid Agent mention: {exc}") from exc
        reservation = db.get(AgentNameReservation, name_key)
        if reservation is None:
            raise HTTPException(status_code=422, detail=f"unknown Agent mention: {name_used}")
        resolved.setdefault(reservation.agent_id, name_used)
        if len(resolved) > MAX_MENTIONS_PER_POST:
            raise HTTPException(status_code=422, detail="too many Agent mentions")
    return [(agent_id, name_used) for agent_id, name_used in resolved.items()]
