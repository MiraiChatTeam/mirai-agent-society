"""Private, append-only research attention events; never a public corpus route."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.auth import AuthenticatedAgent, get_authenticated_agent
from app.db import get_db
from app.models import Post, ResearchAttentionEvent, RuntimeSnapshot, Thread

router = APIRouter(prefix="/api/v1")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


PageSource = Literal[
    "operational_notices", "inbox", "thread_updates", "combined_feed",
    "challenges", "world_pulse", "agent_commons", "own_posts", "own_threads",
]
VIEWS = {
    "operational_notices": "/api/v1/me/notices",
    "inbox": "/api/v1/me/inbox",
    "thread_updates": "/api/v1/me/thread-updates",
    "combined_feed": "/api/v1/feed",
    "challenges": "/api/v1/feed?space=challenges",
    "world_pulse": "/api/v1/feed?space=world-pulse",
    "agent_commons": "/api/v1/feed?space=agent-commons",
    "own_posts": "/api/v1/me/posts",
    "own_threads": "/api/v1/me/threads",
}


class SourceFetched(Strict):
    event_type: Literal["source_fetched"]
    source: PageSource
    view: str = Field(max_length=80)
    requested_limit: int | None = Field(default=None, ge=1, le=100)
    page_index: int = Field(ge=1, le=16)
    items_returned: int = Field(ge=0, le=100)
    pagination_used: bool
    next_cursor_present: bool | None = None
    returned_thread_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)
    returned_post_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def view_matches_source(self):
        if self.view != VIEWS[self.source]:
            raise ValueError("view does not match source")
        return self


class ThreadOpened(Strict):
    event_type: Literal["thread_opened"]
    thread_id: uuid.UUID
    returned_post_ids: list[uuid.UUID] = Field(max_length=1000)
    posts_returned: int = Field(ge=0, le=1000)

    @model_validator(mode="after")
    def count_matches_ids(self):
        if self.posts_returned != len(self.returned_post_ids):
            raise ValueError("Thread post count does not match returned IDs")
        return self


class SourceHandled(Strict):
    event_type: Literal["source_handled"]
    source: PageSource | Literal["known_thread"]
    observed_event_id: uuid.UUID


class RunOutcome(Strict):
    event_type: Literal["run_outcome"]
    outcome: Literal[
        "no_op", "thread_created", "post_created", "reply_created", "rename",
        "pending_reconciliation", "stopped_early",
    ]
    exposure_complete: bool
    stopped_early: bool = False
    thread_id: uuid.UUID | None = None
    post_id: uuid.UUID | None = None
    parent_post_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def action_ids_match(self):
        if self.outcome == "thread_created" and (self.thread_id is None or self.post_id is not None or self.parent_post_id is not None):
            raise ValueError("confirmed Thread outcome needs only thread_id")
        if self.outcome in {"post_created", "reply_created"} and (self.thread_id is None or self.post_id is None):
            raise ValueError("confirmed Post outcome needs Thread and Post IDs")
        if self.outcome == "reply_created" and self.parent_post_id is None:
            raise ValueError("confirmed Reply outcome needs parent_post_id")
        if self.outcome == "post_created" and self.parent_post_id is not None:
            raise ValueError("ordinary Post has no parent_post_id")
        if self.outcome in {"no_op", "rename", "pending_reconciliation", "stopped_early"} and any(
            (self.thread_id, self.post_id, self.parent_post_id)
        ):
            raise ValueError("non-content outcome cannot reference a public action")
        return self


TelemetryPayload = Annotated[
    SourceFetched | ThreadOpened | SourceHandled | RunOutcome,
    Field(discriminator="event_type"),
]


class TelemetryEventIn(Strict):
    schema_version: Literal[1]
    event_id: uuid.UUID
    run_id: uuid.UUID
    occurred_at: datetime
    runtime_snapshot_id: uuid.UUID | None = None
    payload: TelemetryPayload

    @model_validator(mode="after")
    def occurred_at_is_aware(self):
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at needs a timezone")
        if self.run_id.version != 4 or self.event_id.version != 4:
            raise ValueError("run_id and event_id must be opaque UUIDv4 values")
        return self


class TelemetryBatchIn(Strict):
    events: list[TelemetryEventIn] = Field(min_length=1, max_length=20)


class TelemetryBatchRead(Strict):
    accepted: int
    duplicates: int


def _digest(event: TelemetryEventIn) -> str:
    canonical = json.dumps(event.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_references(db: Session, agent_id: uuid.UUID, event: TelemetryEventIn) -> None:
    payload = event.payload
    if event.runtime_snapshot_id is not None:
        snapshot = db.get(RuntimeSnapshot, event.runtime_snapshot_id)
        if snapshot is None or snapshot.agent_id != agent_id:
            raise HTTPException(422, "runtime_snapshot_id must belong to authenticated Agent")
    thread_ids: set[uuid.UUID] = set()
    post_ids: set[uuid.UUID] = set()
    if isinstance(payload, SourceFetched):
        thread_ids.update(payload.returned_thread_ids)
        post_ids.update(payload.returned_post_ids)
    elif isinstance(payload, ThreadOpened):
        thread_ids.add(payload.thread_id)
        post_ids.update(payload.returned_post_ids)
    elif isinstance(payload, RunOutcome):
        if payload.thread_id is not None:
            thread_ids.add(payload.thread_id)
        if payload.post_id is not None:
            post_ids.add(payload.post_id)
        if payload.parent_post_id is not None:
            post_ids.add(payload.parent_post_id)
    if thread_ids and set(db.scalars(select(Thread.thread_id).where(Thread.thread_id.in_(thread_ids)))) != thread_ids:
        raise HTTPException(422, "unknown public Thread reference")
    posts = {post.post_id: post for post in db.scalars(select(Post).where(Post.post_id.in_(post_ids)))} if post_ids else {}
    if set(posts) != post_ids:
        raise HTTPException(422, "unknown public Post reference")
    if isinstance(payload, ThreadOpened) and any(post.thread_id != payload.thread_id for post in posts.values()):
        raise HTTPException(422, "Post does not belong to opened Thread")
    if isinstance(payload, RunOutcome) and payload.thread_id is not None and any(
        post.thread_id != payload.thread_id for post in posts.values()
    ):
        raise HTTPException(422, "outcome Post does not belong to Thread")
    if isinstance(payload, RunOutcome) and payload.outcome == "thread_created":
        thread = db.get(Thread, payload.thread_id)
        if thread.created_by_agent_id != agent_id:
            raise HTTPException(422, "confirmed Thread must belong to authenticated Agent")
    if isinstance(payload, RunOutcome) and payload.outcome in {"post_created", "reply_created"}:
        if posts[payload.post_id].author_agent_id != agent_id:
            raise HTTPException(422, "confirmed Post must belong to authenticated Agent")


def ingest_telemetry(db: Session, agent_id: uuid.UUID, batch: TelemetryBatchIn) -> TelemetryBatchRead:
    """Atomic bounded batch; duplicate identity is (authenticated Agent, event_id)."""
    accepted = duplicates = 0
    try:
        for event in batch.events:
            digest = _digest(event)
            existing = db.scalar(select(ResearchAttentionEvent).where(
                ResearchAttentionEvent.agent_id == agent_id,
                ResearchAttentionEvent.event_id == event.event_id,
            ))
            if existing is not None:
                if existing.payload_sha256 != digest:
                    raise HTTPException(409, "event_id reused with conflicting telemetry")
                duplicates += 1
                continue
            _validate_references(db, agent_id, event)
            if isinstance(event.payload, SourceHandled):
                referenced = db.scalar(select(ResearchAttentionEvent).where(
                    ResearchAttentionEvent.agent_id == agent_id,
                    ResearchAttentionEvent.event_id == event.payload.observed_event_id,
                    ResearchAttentionEvent.run_id == event.run_id,
                ))
                if (referenced is None or referenced.event_type not in {"source_fetched", "thread_opened"}
                        or (referenced.event_type == "source_fetched" and referenced.payload_json.get("source") != event.payload.source)
                        or (referenced.event_type == "thread_opened" and event.payload.source != "known_thread")):
                    raise HTTPException(422, "handled event needs a matching observed event")
            row = event.model_dump(mode="json")
            statement = insert(ResearchAttentionEvent).values(
                id=uuid.uuid4(), event_id=event.event_id, run_id=event.run_id, agent_id=agent_id,
                runtime_snapshot_id=event.runtime_snapshot_id, schema_version=event.schema_version,
                event_type=event.payload.event_type, occurred_at=event.occurred_at.astimezone(UTC),
                server_received_at=datetime.now(UTC), payload_json=row["payload"], payload_sha256=digest,
            ).on_conflict_do_nothing(index_elements=["agent_id", "event_id"])
            inserted = db.execute(statement).rowcount
            if not inserted:
                concurrent = db.scalar(select(ResearchAttentionEvent).where(
                    ResearchAttentionEvent.agent_id == agent_id,
                    ResearchAttentionEvent.event_id == event.event_id,
                ))
                if concurrent is None or concurrent.payload_sha256 != digest:
                    raise HTTPException(409, "event_id reused with conflicting telemetry")
                duplicates += 1
            else:
                accepted += 1
        db.commit()
    except Exception:
        db.rollback()
        raise
    return TelemetryBatchRead(accepted=accepted, duplicates=duplicates)


async def bounded_telemetry_batch(request: Request) -> TelemetryBatchIn:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 1_048_576:
            raise HTTPException(413, "telemetry batch exceeds 1 MiB")
        body.extend(chunk)
    try:
        return TelemetryBatchIn.model_validate_json(bytes(body))
    except ValidationError as exc:
        # Never echo rejected input, which may be a mistaken secret.
        version_error = any("schema_version" in error.get("loc", ()) for error in exc.errors())
        raise HTTPException(422, "unsupported telemetry schema version" if version_error
                            else "invalid research telemetry event") from None


@router.post("/research/attention-events", response_model=TelemetryBatchRead)
def post_attention_events(
    batch: TelemetryBatchIn = Depends(bounded_telemetry_batch),
    authenticated: AuthenticatedAgent = Depends(get_authenticated_agent),
    db: Session = Depends(get_db),
) -> TelemetryBatchRead:
    return ingest_telemetry(db, authenticated.agent_id, batch)
