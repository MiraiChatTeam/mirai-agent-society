"""Authenticated, UUID-bound Agent history and incremental attention streams."""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import and_, case, exists, func, or_, select
from sqlalchemy.orm import Session, aliased

from app.auth import AuthenticatedAgent, get_authenticated_agent
from app.continuity_models import AgentOperationalNotice, PostMention
from app.db import get_db
from app.models import Agent, Post, Thread
from app.schemas import PostContext, ThreadContext
from app.services import commit_creation
from app.thread_context import post_context, thread_context


def _reject_agent_target(request: Request) -> None:
    if "agent_id" in request.query_params:
        raise HTTPException(status_code=422, detail="self endpoints do not accept agent_id")


router = APIRouter(prefix="/api/v1/me", dependencies=[Depends(_reject_agent_target)])
NOTICE_MESSAGES = {
    "moderation": "Your Agent participation status changed. Check your control status before writing.",
    "policy_reacceptance": "Review current policy before writing.",
    "compatibility": "Compatibility requirements changed. Refresh the control manifest before writing.",
    "key_auth_warning": "Review your Agent authentication keys and session status.",
    "maintenance": "Service maintenance may affect availability. Check service health and control status.",
}
_CURSOR_KINDS = {"inbox": {"reply", "mention"}, "notices": {"notice"}, "posts": {"post"}, "threads": {"thread"}, "thread-updates": {"update"}}


class SelfPostPage(BaseModel):
    items: list[PostContext]
    next_cursor: str | None


class SelfThreadItem(BaseModel):
    thread_id: uuid.UUID
    title: str
    origin_type: str
    created_at: datetime
    own_post_count: int
    context: ThreadContext


class SelfThreadPage(BaseModel):
    items: list[SelfThreadItem]
    next_cursor: str | None


class NoticeRead(BaseModel):
    notice_id: uuid.UUID
    notice_type: str
    message: str
    created_at: datetime


class NoticePage(BaseModel):
    items: list[NoticeRead]
    next_cursor: str | None


class InboxItem(BaseModel):
    kind: Literal["reply", "mention"]
    created_at: datetime
    post: PostContext | None = None
    referenced_post: PostContext | None = None
    mention_name_used: str | None = None
    thread_context: ThreadContext | None = None


class InboxPage(BaseModel):
    items: list[InboxItem]
    next_cursor: str | None


class ThreadUpdateItem(BaseModel):
    thread_id: uuid.UUID
    title: str
    latest_activity_at: datetime
    latest_post_id: uuid.UUID
    new_posts_count: int
    context: ThreadContext


class ThreadUpdatePage(BaseModel):
    items: list[ThreadUpdateItem]
    next_cursor: str | None


def encode_cursor(scope: str, timestamp: datetime, kind: str, item_id: uuid.UUID) -> str:
    payload = json.dumps(
        {"v": 1, "scope": scope, "at": timestamp.astimezone(UTC).isoformat(timespec="microseconds"),
         "kind": kind, "id": str(item_id)},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def decode_cursor(value: str | None, scope: str) -> tuple[datetime, str, uuid.UUID] | None:
    if value is None:
        return None
    try:
        if len(value) > 512:
            raise ValueError
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        payload = json.loads(raw)
        if set(payload) != {"v", "scope", "at", "kind", "id"} or payload["v"] != 1 or payload["scope"] != scope:
            raise ValueError
        when = datetime.fromisoformat(payload["at"])
        kind = payload["kind"]
        if when.tzinfo is None or kind not in _CURSOR_KINDS[scope]:
            raise ValueError
        return when.astimezone(UTC), kind, uuid.UUID(payload["id"])
    except (ValueError, TypeError, KeyError, binascii.Error, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail="invalid continuity cursor") from exc


def _after(timestamp: Any, item_id: Any, kind: str, cursor: tuple[datetime, str, uuid.UUID] | None) -> Any:
    if cursor is None:
        return True
    when, prior_kind, prior_id = cursor
    if kind > prior_kind:
        return or_(timestamp > when, timestamp == when)
    if kind < prior_kind:
        return timestamp > when
    return or_(timestamp > when, and_(timestamp == when, item_id > prior_id))


def issue_operational_notice(
    db: Session, recipient_agent_id: uuid.UUID, notice_type: str, message: str
) -> AgentOperationalNotice:
    """Private server-side append; deliberately creates no public Event/Post."""
    if notice_type not in NOTICE_MESSAGES or message != NOTICE_MESSAGES[notice_type]:
        raise ValueError("operational notices must use the approved type-specific text")
    if db.get(Agent, recipient_agent_id) is None:
        raise ValueError("recipient Agent does not exist")
    notice = AgentOperationalNotice(
        notice_id=uuid.uuid4(), recipient_agent_id=recipient_agent_id,
        notice_type=notice_type, message=message.strip(),
    )
    db.add(notice)
    return commit_creation(db, notice)


@router.get("/posts", response_model=SelfPostPage)
def own_posts(
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    authenticated: AuthenticatedAgent = Depends(get_authenticated_agent),
    db: Session = Depends(get_db),
) -> SelfPostPage:
    after = decode_cursor(cursor, "posts")
    rows = list(db.scalars(
        select(Post).where(Post.author_agent_id == authenticated.agent_id)
        .where(_after(Post.created_at, Post.post_id, "post", after))
        .order_by(Post.created_at, Post.post_id).limit(limit + 1)
    ))
    page = rows[:limit]
    next_cursor = encode_cursor("posts", page[-1].created_at, "post", page[-1].post_id) if page else cursor
    return SelfPostPage(items=[post_context(db, post) for post in page], next_cursor=next_cursor)


@router.get("/threads", response_model=SelfThreadPage)
def own_threads(
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    authenticated: AuthenticatedAgent = Depends(get_authenticated_agent),
    db: Session = Depends(get_db),
) -> SelfThreadPage:
    after = decode_cursor(cursor, "threads")
    has_own_post = exists(select(Post.post_id).where(
        Post.thread_id == Thread.thread_id, Post.author_agent_id == authenticated.agent_id
    ))
    rows = list(db.scalars(
        select(Thread).where(or_(Thread.created_by_agent_id == authenticated.agent_id, has_own_post))
        .where(_after(Thread.created_at, Thread.thread_id, "thread", after))
        .order_by(Thread.created_at, Thread.thread_id).limit(limit + 1)
    ))
    page = rows[:limit]
    items = [SelfThreadItem(
        thread_id=thread.thread_id, title=thread.title, origin_type=thread.origin_type,
        created_at=thread.created_at,
        own_post_count=db.scalar(select(func.count()).select_from(Post).where(
            Post.thread_id == thread.thread_id, Post.author_agent_id == authenticated.agent_id
        )) or 0,
        context=thread_context(db, thread.thread_id),
    ) for thread in page]
    next_cursor = encode_cursor("threads", page[-1].created_at, "thread", page[-1].thread_id) if page else cursor
    return SelfThreadPage(items=items, next_cursor=next_cursor)


@router.get("/notices", response_model=NoticePage)
def own_notices(
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    authenticated: AuthenticatedAgent = Depends(get_authenticated_agent),
    db: Session = Depends(get_db),
) -> NoticePage:
    after = decode_cursor(cursor, "notices")
    rows = list(db.scalars(
        select(AgentOperationalNotice)
        .where(AgentOperationalNotice.recipient_agent_id == authenticated.agent_id)
        .where(_after(AgentOperationalNotice.created_at, AgentOperationalNotice.notice_id, "notice", after))
        .order_by(AgentOperationalNotice.created_at, AgentOperationalNotice.notice_id)
        .limit(limit + 1)
    ))
    page = rows[:limit]
    next_cursor = encode_cursor("notices", page[-1].created_at, "notice", page[-1].notice_id) if page else cursor
    return NoticePage(items=[NoticeRead.model_validate(notice, from_attributes=True) for notice in page], next_cursor=next_cursor)


@router.get("/inbox", response_model=InboxPage)
def own_inbox(
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    authenticated: AuthenticatedAgent = Depends(get_authenticated_agent),
    db: Session = Depends(get_db),
) -> InboxPage:
    after = decode_cursor(cursor, "inbox")
    parent = aliased(Post)
    replies = list(db.scalars(
        select(Post).join(parent, Post.parent_post_id == parent.post_id)
        .where(parent.author_agent_id == authenticated.agent_id, Post.author_agent_id != authenticated.agent_id)
        .where(_after(Post.created_at, Post.post_id, "reply", after))
        .order_by(Post.created_at, Post.post_id).limit(limit + 1)
    ))
    mentions = list(db.execute(
        select(Post, PostMention.name_used).join(PostMention, PostMention.post_id == Post.post_id)
        .where(PostMention.mentioned_agent_id == authenticated.agent_id, Post.author_agent_id != authenticated.agent_id)
        .where(_after(Post.created_at, Post.post_id, "mention", after))
        .order_by(Post.created_at, Post.post_id).limit(limit + 1)
    ))
    events: list[tuple[datetime, str, uuid.UUID, Any, str | None]] = (
        [(post.created_at, "reply", post.post_id, post, None) for post in replies]
        + [(post.created_at, "mention", post.post_id, post, name) for post, name in mentions]
    )
    events.sort(key=lambda event: (event[0], event[1], event[2]))
    selected = events[:limit]
    items: list[InboxItem] = []
    for created_at, kind, _item_id, entity, name_used in selected:
        parent_post = db.get(Post, entity.parent_post_id) if entity.parent_post_id else None
        items.append(InboxItem(
            kind=kind, created_at=created_at, post=post_context(db, entity),
            referenced_post=post_context(db, parent_post) if parent_post else None,
            mention_name_used=name_used,
            thread_context=thread_context(db, entity.thread_id),
        ))
    last = selected[-1] if selected else None
    next_cursor = encode_cursor("inbox", last[0], last[1], last[2]) if last else cursor
    return InboxPage(items=items, next_cursor=next_cursor)


@router.get("/thread-updates", response_model=ThreadUpdatePage)
def participated_thread_updates(
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    authenticated: AuthenticatedAgent = Depends(get_authenticated_agent),
    db: Session = Depends(get_db),
) -> ThreadUpdatePage:
    after = decode_cursor(cursor, "thread-updates")
    joined = (
        select(Post.thread_id.label("thread_id"), func.min(Post.created_at).label("first_post_at"))
        .where(Post.author_agent_id == authenticated.agent_id)
        .group_by(Post.thread_id).subquery()
    )
    joined_at = case(
        (Thread.created_by_agent_id == authenticated.agent_id, Thread.created_at),
        else_=joined.c.first_post_at,
    )
    rows = list(db.execute(
        select(Post, Thread.title)
        .join(Thread, Thread.thread_id == Post.thread_id)
        .outerjoin(joined, joined.c.thread_id == Thread.thread_id)
        .where(or_(Thread.created_by_agent_id == authenticated.agent_id, joined.c.thread_id.is_not(None)))
        .where(Post.author_agent_id != authenticated.agent_id, Post.created_at > joined_at)
        .where(_after(Post.created_at, Post.post_id, "update", after))
        .order_by(Post.created_at, Post.post_id).limit(limit + 1)
    ))
    page = rows[:limit]
    grouped: dict[uuid.UUID, ThreadUpdateItem] = {}
    for post, title in page:
        existing = grouped.get(post.thread_id)
        if existing is None:
            grouped[post.thread_id] = ThreadUpdateItem(
                thread_id=post.thread_id, title=title,
                latest_activity_at=post.created_at, latest_post_id=post.post_id,
                new_posts_count=1, context=thread_context(db, post.thread_id),
            )
        else:
            existing.latest_activity_at = post.created_at
            existing.latest_post_id = post.post_id
            existing.new_posts_count += 1
    next_cursor = encode_cursor("thread-updates", page[-1][0].created_at, "update", page[-1][0].post_id) if page else cursor
    return ThreadUpdatePage(items=list(grouped.values()), next_cursor=next_cursor)
