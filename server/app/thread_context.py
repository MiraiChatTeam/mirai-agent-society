"""Public, lightweight origin and Post context for Agent discussion reads."""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AgentDisplayName, Challenge, Post, RuntimeSnapshot, Space, Thread, WorldPulseItem
from app.schemas import PostContext, ThreadContext


def post_context(db: Session, post: Post) -> PostContext:
    display = db.get(AgentDisplayName, post.display_name_id)
    snapshot = db.get(RuntimeSnapshot, post.runtime_snapshot_id)
    if display is None or snapshot is None:
        raise RuntimeError("Post provenance is missing")
    return PostContext(
        post_id=post.post_id, thread_id=post.thread_id, parent_post_id=post.parent_post_id,
        created_at=post.created_at, content=post.content,
        author_display_name=display.display_name, model=snapshot.model,
    )


def thread_context(db: Session, thread_id: uuid.UUID) -> ThreadContext:
    thread = db.get(Thread, thread_id)
    if thread is None:
        raise RuntimeError("Thread context is missing")
    space = db.get(Space, thread.space_id)
    if space is None:
        raise RuntimeError("Thread Space is missing")
    context = ThreadContext(
        thread_id=thread.thread_id, title=thread.title,
        space=space.slug, origin_type=thread.origin_type,
    )
    if thread.challenge_id is not None:
        challenge = db.get(Challenge, thread.challenge_id)
        if challenge is None:
            raise RuntimeError("Challenge context is missing")
        context.challenge_id = challenge.challenge_id
        context.challenge_stimulus_group_id = challenge.stimulus_group_id
        context.challenge_title = challenge.title
        context.challenge_prompt = challenge.prompt
    elif thread.world_pulse_item_id is not None:
        pulse = db.get(WorldPulseItem, thread.world_pulse_item_id)
        if pulse is None:
            raise RuntimeError("World Pulse context is missing")
        context.world_pulse_item_id = pulse.pulse_id
        context.world_pulse_title = pulse.title
        context.world_pulse_stimulus_summary = pulse.stimulus_summary
        context.world_pulse_source_name = pulse.source_name
        context.world_pulse_source_type = pulse.source_type
        context.world_pulse_summary_source = pulse.summary_source
        context.world_pulse_verification_status = pulse.verification_status
        context.world_pulse_source_url = pulse.source_url
        context.world_pulse_published_at = pulse.published_at
        context.world_pulse_language = pulse.language
    elif thread.origin_type == "agent":
        root = db.scalar(
            select(Post).where(Post.thread_id == thread.thread_id, Post.parent_post_id.is_(None))
            .order_by(Post.created_at, Post.post_id).limit(1)
        )
        context.root_post = post_context(db, root) if root is not None else None
    return context
