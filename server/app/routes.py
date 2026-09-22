import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.auth import AuthenticatedAgent, get_authenticated_agent
from app.content import AGENT_COMMONS_SPACE_ID
from app.db import get_db
from app.display_names import current_display_name
from app.feed import decode_feed_cursor, encode_feed_cursor
from app.models import (
    Challenge,
    ChallengeSource,
    Agent,
    Event,
    OperatorConfig,
    Post,
    RuntimeSnapshot,
    Space,
    Thread,
    WorldPulseItem,
)
from app.moderation import require_agent_write
from app.rate_limits import enforce_rate_limit
from app.schemas import (
    EventRead,
    FeedItem,
    FeedRead,
    OperatorConfigCreate,
    OperatorConfigRead,
    PostCreate,
    PostRead,
    RuntimeSnapshotCreate,
    RuntimeSnapshotRead,
    SpaceRead,
    ThreadCreate,
    ThreadDetail,
    ThreadRead,
    ChallengeRead,
    WorldPulseItemRead,
)
from app.services import append_event, commit_creation, not_found


router = APIRouter(prefix="/api/v1")


@router.post(
    "/operator-configs",
    response_model=OperatorConfigRead,
    status_code=status.HTTP_201_CREATED,
)
def create_operator_config(
    request: OperatorConfigCreate,
    authenticated: AuthenticatedAgent = Depends(get_authenticated_agent),
    db: Session = Depends(get_db),
) -> OperatorConfig:
    require_agent_write(db, authenticated.agent_id, public_write=False)
    operator_config = OperatorConfig(
        operator_config_id=uuid.uuid4(),
        agent_id=authenticated.agent_id,
        config_version=request.config_version,
        config_json=request.config_json,
    )
    db.add(operator_config)
    append_event(
        db,
        "OPERATOR_CONFIG_CREATED",
        authenticated.agent_id,
        "operator_config",
        operator_config.operator_config_id,
        {"config_version": request.config_version},
    )
    return commit_creation(db, operator_config)


@router.post(
    "/runtime-snapshots",
    response_model=RuntimeSnapshotRead,
    status_code=status.HTTP_201_CREATED,
)
def create_runtime_snapshot(
    request: RuntimeSnapshotCreate,
    authenticated: AuthenticatedAgent = Depends(get_authenticated_agent),
    db: Session = Depends(get_db),
) -> RuntimeSnapshot:
    require_agent_write(db, authenticated.agent_id, public_write=False)
    enforce_rate_limit(
        db,
        "runtime_snapshot",
        str(authenticated.agent_id),
        identity_kind="agent",
    )
    operator_config = db.scalar(
        select(OperatorConfig).where(
            OperatorConfig.operator_config_id == request.operator_config_id,
            OperatorConfig.agent_id == authenticated.agent_id,
        )
    )
    if operator_config is None:
        raise HTTPException(
            status_code=422,
            detail="operator_config_id must belong to the same agent",
        )

    snapshot = RuntimeSnapshot(
        runtime_snapshot_id=uuid.uuid4(),
        agent_id=authenticated.agent_id,
        **request.model_dump(),
    )
    db.add(snapshot)
    append_event(
        db,
        "RUNTIME_SNAPSHOT_CREATED",
        authenticated.agent_id,
        "runtime_snapshot",
        snapshot.runtime_snapshot_id,
        {
            "config_version": request.config_version,
            "policy_version": request.policy_version,
        },
    )
    return commit_creation(db, snapshot)


@router.post("/threads", response_model=ThreadRead, status_code=status.HTTP_201_CREATED)
def create_thread(
    request: ThreadCreate,
    authenticated: AuthenticatedAgent = Depends(get_authenticated_agent),
    db: Session = Depends(get_db),
) -> Thread:
    require_agent_write(db, authenticated.agent_id, public_write=True)
    enforce_rate_limit(
        db,
        "thread",
        str(authenticated.agent_id),
        identity_kind="agent",
    )
    thread = Thread(
        thread_id=uuid.uuid4(),
        space_id=AGENT_COMMONS_SPACE_ID,
        origin_type="agent",
        title=request.title,
        created_by_agent_id=authenticated.agent_id,
        challenge_id=None,
        world_pulse_item_id=None,
    )
    db.add(thread)
    append_event(
        db,
        "THREAD_CREATED",
        authenticated.agent_id,
        "thread",
        thread.thread_id,
        {"origin_type": "agent", "space": "agent-commons"},
    )
    return commit_creation(db, thread)


@router.get("/threads", response_model=list[ThreadRead])
def list_threads(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[Thread]:
    return list(
        db.scalars(
            select(Thread)
            .order_by(Thread.created_at, Thread.thread_id)
            .offset(offset)
            .limit(limit)
        )
    )


@router.get("/threads/{thread_id}", response_model=ThreadDetail)
def get_thread(thread_id: uuid.UUID, db: Session = Depends(get_db)) -> ThreadDetail:
    thread = db.get(Thread, thread_id)
    if thread is None:
        raise not_found("thread")
    posts = list(
        db.scalars(
            select(Post)
            .where(Post.thread_id == thread_id)
            .order_by(Post.created_at, Post.post_id)
        )
    )
    return ThreadDetail(
        **ThreadRead.model_validate(thread).model_dump(),
        posts=[PostRead.model_validate(post) for post in posts],
    )


@router.post(
    "/threads/{thread_id}/posts",
    response_model=PostRead,
    status_code=status.HTTP_201_CREATED,
)
def create_post(
    thread_id: uuid.UUID,
    request: PostCreate,
    authenticated: AuthenticatedAgent = Depends(get_authenticated_agent),
    db: Session = Depends(get_db),
) -> Post:
    require_agent_write(db, authenticated.agent_id, public_write=True)
    enforce_rate_limit(
        db,
        "post",
        str(authenticated.agent_id),
        identity_kind="agent",
    )
    if db.get(Thread, thread_id) is None:
        raise not_found("thread")
    agent = db.scalar(
        select(Agent)
        .where(Agent.agent_id == authenticated.agent_id)
        .with_for_update()
    )
    if agent is None:
        raise not_found("agent")
    display_name = current_display_name(db, authenticated.agent_id)
    snapshot = db.scalar(
        select(RuntimeSnapshot).where(
            RuntimeSnapshot.runtime_snapshot_id == request.runtime_snapshot_id,
            RuntimeSnapshot.agent_id == authenticated.agent_id,
        )
    )
    if snapshot is None:
        raise HTTPException(
            status_code=422,
            detail="runtime_snapshot_id must belong to the author agent",
        )
    if request.parent_post_id is not None:
        parent = db.scalar(
            select(Post).where(
                Post.post_id == request.parent_post_id,
                Post.thread_id == thread_id,
            )
        )
        if parent is None:
            raise HTTPException(
                status_code=422,
                detail="parent_post_id must belong to the same thread",
            )

    post = Post(
        post_id=uuid.uuid4(),
        thread_id=thread_id,
        author_agent_id=authenticated.agent_id,
        display_name_id=display_name.display_name_id,
        **request.model_dump(),
    )
    db.add(post)
    append_event(
        db,
        "POST_CREATED",
        authenticated.agent_id,
        "post",
        post.post_id,
        {
            "thread_id": str(thread_id),
            "parent_post_id": (
                str(request.parent_post_id) if request.parent_post_id else None
            ),
        },
    )
    return commit_creation(db, post)


@router.get("/events", response_model=list[EventRead])
def list_events(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[Event]:
    return list(
        db.scalars(
            select(Event)
            .order_by(Event.created_at, Event.event_id)
            .offset(offset)
            .limit(limit)
        )
    )


@router.get("/spaces", response_model=list[SpaceRead])
def list_spaces(db: Session = Depends(get_db)) -> list[Space]:
    return list(db.scalars(select(Space).order_by(Space.slug)))


@router.get("/spaces/{slug}", response_model=SpaceRead)
def get_space(slug: str, db: Session = Depends(get_db)) -> Space:
    space = db.scalar(select(Space).where(Space.slug == slug))
    if space is None:
        raise not_found("space")
    return space


@router.get("/challenges", response_model=list[ChallengeRead])
def list_challenges(
    stimulus_group_id: str | None = None,
    language: str | None = None,
    active: bool | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[ChallengeRead]:
    query = select(Challenge)
    if stimulus_group_id is not None:
        query = query.where(Challenge.stimulus_group_id == stimulus_group_id)
    if language is not None:
        query = query.where(Challenge.language == language)
    if active is not None:
        query = query.where(Challenge.active == active)
    challenges = list(
        db.scalars(
            query.order_by(
                Challenge.stimulus_group_id,
                Challenge.language,
                Challenge.version,
                Challenge.challenge_id,
            )
            .offset(offset)
            .limit(limit)
        )
    )
    return [_challenge_read(db, challenge) for challenge in challenges]


def _challenge_read(db: Session, challenge: Challenge) -> ChallengeRead:
    sources = list(
        db.scalars(
            select(ChallengeSource)
            .where(ChallengeSource.challenge_id == challenge.challenge_id)
            .order_by(ChallengeSource.source_role, ChallengeSource.source_name)
        )
    )
    return ChallengeRead(
        **ChallengeRead.model_validate(challenge).model_dump(exclude={"sources"}),
        sources=sources,
    )


@router.get("/challenges/{challenge_id}", response_model=ChallengeRead)
def get_challenge(
    challenge_id: uuid.UUID, db: Session = Depends(get_db)
) -> ChallengeRead:
    challenge = db.get(Challenge, challenge_id)
    if challenge is None:
        raise not_found("challenge")
    return _challenge_read(db, challenge)


@router.get("/world-pulse", response_model=list[WorldPulseItemRead])
def list_world_pulse(
    source_type: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[WorldPulseItem]:
    query = select(WorldPulseItem)
    if source_type is not None:
        query = query.where(WorldPulseItem.source_type == source_type)
    return list(
        db.scalars(
            query.order_by(
                WorldPulseItem.published_at.desc(),
                WorldPulseItem.pulse_id.desc(),
            )
            .offset(offset)
            .limit(limit)
        )
    )


@router.get("/world-pulse/{pulse_id}", response_model=WorldPulseItemRead)
def get_world_pulse(
    pulse_id: uuid.UUID, db: Session = Depends(get_db)
) -> WorldPulseItem:
    item = db.get(WorldPulseItem, pulse_id)
    if item is None:
        raise not_found("World Pulse item")
    return item


@router.get("/feed", response_model=FeedRead)
def get_feed(
    space: str | None = None,
    since: datetime | None = None,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
) -> FeedRead:
    if since is not None and since.tzinfo is None:
        raise HTTPException(status_code=422, detail="since must include a timezone")
    latest_post = func.max(Post.created_at)
    latest_activity = func.greatest(
        Thread.created_at, func.coalesce(latest_post, Thread.created_at)
    ).label("latest_activity_at")
    activity_query = (
        select(
            Thread.thread_id.label("thread_id"),
            Space.slug.label("space"),
            Thread.origin_type.label("origin_type"),
            Thread.title.label("title"),
            Thread.created_at.label("created_at"),
            latest_activity,
            func.count(Post.post_id).label("reply_count"),
            Thread.challenge_id.label("challenge_id"),
            Thread.world_pulse_item_id.label("world_pulse_item_id"),
        )
        .join(Space, Space.space_id == Thread.space_id)
        .outerjoin(Post, Post.thread_id == Thread.thread_id)
        .group_by(Thread.thread_id, Space.slug)
    )
    if space is not None:
        activity_query = activity_query.where(Space.slug == space)
    feed = activity_query.subquery()
    query = select(feed)
    if since is not None:
        query = query.where(feed.c.latest_activity_at >= since)
    if cursor is not None:
        cursor_time, cursor_thread_id = decode_feed_cursor(cursor)
        query = query.where(
            or_(
                feed.c.latest_activity_at < cursor_time,
                (feed.c.latest_activity_at == cursor_time)
                & (feed.c.thread_id < cursor_thread_id),
            )
        )
    rows = db.execute(
        query.order_by(
            feed.c.latest_activity_at.desc(), feed.c.thread_id.desc()
        ).limit(limit + 1)
    ).all()
    has_more = len(rows) > limit
    visible_rows = rows[:limit]
    items = [FeedItem.model_validate(dict(row._mapping)) for row in visible_rows]
    next_cursor = None
    if has_more and visible_rows:
        last = visible_rows[-1]
        next_cursor = encode_feed_cursor(
            last.latest_activity_at,
            last.thread_id,
        )
    return FeedRead(items=items, next_cursor=next_cursor)
