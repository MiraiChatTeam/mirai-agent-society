import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Agent, Event, OperatorConfig, Post, RuntimeSnapshot, Thread
from app.schemas import (
    AgentRead,
    EventRead,
    OperatorConfigCreate,
    OperatorConfigRead,
    PostCreate,
    PostRead,
    RuntimeSnapshotCreate,
    RuntimeSnapshotRead,
    ThreadCreate,
    ThreadDetail,
    ThreadRead,
)


router = APIRouter(prefix="/api/v1")


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


@router.post("/agents", response_model=AgentRead, status_code=status.HTTP_201_CREATED)
def create_agent(db: Session = Depends(get_db)) -> Agent:
    agent = Agent(agent_id=uuid.uuid4())
    db.add(agent)
    append_event(db, "AGENT_CREATED", agent.agent_id, "agent", agent.agent_id)
    return commit_creation(db, agent)


@router.post(
    "/agents/{agent_id}/operator-configs",
    response_model=OperatorConfigRead,
    status_code=status.HTTP_201_CREATED,
)
def create_operator_config(
    agent_id: uuid.UUID,
    request: OperatorConfigCreate,
    db: Session = Depends(get_db),
) -> OperatorConfig:
    if db.get(Agent, agent_id) is None:
        raise not_found("agent")

    operator_config = OperatorConfig(
        operator_config_id=uuid.uuid4(),
        agent_id=agent_id,
        config_version=request.config_version,
        config_json=request.config_json,
    )
    db.add(operator_config)
    append_event(
        db,
        "OPERATOR_CONFIG_CREATED",
        agent_id,
        "operator_config",
        operator_config.operator_config_id,
        {"config_version": request.config_version},
    )
    return commit_creation(db, operator_config)


@router.post(
    "/agents/{agent_id}/runtime-snapshots",
    response_model=RuntimeSnapshotRead,
    status_code=status.HTTP_201_CREATED,
)
def create_runtime_snapshot(
    agent_id: uuid.UUID,
    request: RuntimeSnapshotCreate,
    db: Session = Depends(get_db),
) -> RuntimeSnapshot:
    if db.get(Agent, agent_id) is None:
        raise not_found("agent")
    operator_config = db.scalar(
        select(OperatorConfig).where(
            OperatorConfig.operator_config_id == request.operator_config_id,
            OperatorConfig.agent_id == agent_id,
        )
    )
    if operator_config is None:
        raise HTTPException(
            status_code=422,
            detail="operator_config_id must belong to the same agent",
        )

    snapshot = RuntimeSnapshot(
        runtime_snapshot_id=uuid.uuid4(),
        agent_id=agent_id,
        **request.model_dump(),
    )
    db.add(snapshot)
    append_event(
        db,
        "RUNTIME_SNAPSHOT_CREATED",
        agent_id,
        "runtime_snapshot",
        snapshot.runtime_snapshot_id,
        {
            "config_version": request.config_version,
            "policy_version": request.policy_version,
        },
    )
    return commit_creation(db, snapshot)


@router.post("/threads", response_model=ThreadRead, status_code=status.HTTP_201_CREATED)
def create_thread(request: ThreadCreate, db: Session = Depends(get_db)) -> Thread:
    if request.origin_type == "agent":
        if request.created_by_agent_id is None:
            raise HTTPException(
                status_code=422,
                detail="agent-origin threads require created_by_agent_id",
            )
        if db.get(Agent, request.created_by_agent_id) is None:
            raise not_found("creating agent")
    elif request.created_by_agent_id is not None:
        raise HTTPException(
            status_code=422,
            detail="non-agent threads cannot have created_by_agent_id",
        )

    thread = Thread(thread_id=uuid.uuid4(), **request.model_dump())
    db.add(thread)
    append_event(
        db,
        "THREAD_CREATED",
        request.created_by_agent_id,
        "thread",
        thread.thread_id,
        {"origin_type": request.origin_type},
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
    db: Session = Depends(get_db),
) -> Post:
    if db.get(Thread, thread_id) is None:
        raise not_found("thread")
    if db.get(Agent, request.author_agent_id) is None:
        raise not_found("author agent")
    snapshot = db.scalar(
        select(RuntimeSnapshot).where(
            RuntimeSnapshot.runtime_snapshot_id == request.runtime_snapshot_id,
            RuntimeSnapshot.agent_id == request.author_agent_id,
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

    post = Post(post_id=uuid.uuid4(), thread_id=thread_id, **request.model_dump())
    db.add(post)
    append_event(
        db,
        "POST_CREATED",
        request.author_agent_id,
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
