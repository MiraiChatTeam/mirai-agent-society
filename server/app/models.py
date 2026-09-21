import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Agent(Timestamped, Base):
    __tablename__ = "agents"

    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)


class RegistrationInvite(Timestamped, Base):
    __tablename__ = "registration_invites"
    __table_args__ = (
        CheckConstraint("max_uses > 0", name="ck_registration_invite_max_uses"),
        CheckConstraint(
            "use_count >= 0 AND use_count <= max_uses",
            name="ck_registration_invite_use_count",
        ),
        Index("ix_registration_invites_expires", "expires_at"),
    )

    invite_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    max_uses: Mapped[int] = mapped_column(nullable=False)
    use_count: Mapped[int] = mapped_column(nullable=False, default=0)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    label: Mapped[str | None] = mapped_column(String(100), nullable=True)


class RateLimitBucket(Base):
    __tablename__ = "rate_limit_buckets"
    __table_args__ = (
        Index("ix_rate_limit_buckets_updated", "updated_at"),
    )

    rate_limit_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    identity_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    window_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    request_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class AgentModerationAction(Timestamped, Base):
    __tablename__ = "agent_moderation_actions"
    __table_args__ = (
        CheckConstraint(
            "action IN ('muted', 'unmuted', 'suspended', 'restored')",
            name="ck_agent_moderation_action",
        ),
        CheckConstraint(
            "effective_until IS NULL OR effective_until > created_at",
            name="ck_agent_moderation_action_until",
        ),
        Index("ix_agent_moderation_actions_agent_created", "agent_id", "created_at"),
    )

    moderation_action_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.agent_id"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    effective_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)


class AgentModerationState(Base):
    __tablename__ = "agent_moderation_states"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'muted', 'suspended')",
            name="ck_agent_moderation_state",
        ),
        CheckConstraint(
            "status = 'muted' OR muted_until IS NULL",
            name="ck_agent_moderation_muted_until",
        ),
    )

    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.agent_id"), primary_key=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    muted_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AgentKey(Timestamped, Base):
    __tablename__ = "agent_keys"
    __table_args__ = (
        UniqueConstraint("agent_key_id", "agent_id"),
        UniqueConstraint("public_key"),
        UniqueConstraint("fingerprint_sha256"),
        CheckConstraint(
            "expires_at IS NULL OR expires_at > valid_from",
            name="ck_agent_key_expiry",
        ),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= valid_from",
            name="ck_agent_key_revocation",
        ),
        Index("ix_agent_keys_agent_created", "agent_id", "created_at"),
    )

    agent_key_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.agent_id"), nullable=False
    )
    public_key: Mapped[str] = mapped_column(String(44), nullable=False)
    fingerprint_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    key_label: Mapped[str | None] = mapped_column(String(100), nullable=True)


class AuthChallenge(Timestamped, Base):
    __tablename__ = "auth_challenges"
    __table_args__ = (
        ForeignKeyConstraint(
            ["agent_key_id", "agent_id"],
            ["agent_keys.agent_key_id", "agent_keys.agent_id"],
        ),
        CheckConstraint("expires_at > issued_at", name="ck_auth_challenge_expiry"),
        Index("ix_auth_challenges_expires", "expires_at"),
    )

    challenge_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    nonce: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.agent_id"), nullable=False
    )
    agent_key_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AgentSession(Timestamped, Base):
    __tablename__ = "agent_sessions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["agent_key_id", "agent_id"],
            ["agent_keys.agent_key_id", "agent_keys.agent_id"],
        ),
        UniqueConstraint("token_hash"),
        CheckConstraint("expires_at > created_at", name="ck_agent_session_expiry"),
        Index("ix_agent_sessions_agent_expires", "agent_id", "expires_at"),
        Index("ix_agent_sessions_key_active", "agent_key_id", "revoked_at"),
    )

    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.agent_id"), nullable=False
    )
    agent_key_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class OperatorConfig(Timestamped, Base):
    __tablename__ = "operator_configs"
    __table_args__ = (
        UniqueConstraint("operator_config_id", "agent_id"),
        Index("ix_operator_configs_agent_created", "agent_id", "created_at"),
    )

    operator_config_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.agent_id"), nullable=False
    )
    config_version: Mapped[str] = mapped_column(String(32), nullable=False)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class RuntimeSnapshot(Timestamped, Base):
    __tablename__ = "runtime_snapshots"
    __table_args__ = (
        ForeignKeyConstraint(
            ["operator_config_id", "agent_id"],
            ["operator_configs.operator_config_id", "operator_configs.agent_id"],
        ),
        UniqueConstraint("runtime_snapshot_id", "agent_id"),
        CheckConstraint(
            "execution_mode IN ('autonomous', 'scheduled_local', "
            "'provider_scheduled', 'human_triggered', 'unknown')",
            name="ck_runtime_execution_mode",
        ),
        CheckConstraint(
            "web_access IN ('available', 'unavailable', 'unknown')",
            name="ck_runtime_web_access",
        ),
        CheckConstraint(
            "tool_access IN ('available', 'unavailable', 'unknown')",
            name="ck_runtime_tool_access",
        ),
        CheckConstraint(
            "memory_mode IN ('none', 'session', 'persistent', "
            "'external_rag', 'unknown')",
            name="ck_runtime_memory_mode",
        ),
        Index("ix_runtime_snapshots_agent_created", "agent_id", "created_at"),
    )

    runtime_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.agent_id"), nullable=False
    )
    operator_config_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    runtime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    web_access: Mapped[str] = mapped_column(String(16), nullable=False)
    tool_access: Mapped[str] = mapped_column(String(16), nullable=False)
    memory_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    config_version: Mapped[str] = mapped_column(String(32), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(32), nullable=False)


class Thread(Timestamped, Base):
    __tablename__ = "threads"
    __table_args__ = (
        CheckConstraint(
            "origin_type IN ('agent', 'system', 'world_pulse', 'experiment')",
            name="ck_thread_origin_type",
        ),
        CheckConstraint(
            "(origin_type = 'agent' AND created_by_agent_id IS NOT NULL) OR "
            "(origin_type <> 'agent' AND created_by_agent_id IS NULL)",
            name="ck_thread_origin_actor",
        ),
        Index("ix_threads_created_at", "created_at"),
    )

    thread_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    origin_type: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    created_by_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.agent_id"), nullable=True
    )


class Post(Timestamped, Base):
    __tablename__ = "posts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["runtime_snapshot_id", "author_agent_id"],
            ["runtime_snapshots.runtime_snapshot_id", "runtime_snapshots.agent_id"],
        ),
        ForeignKeyConstraint(
            ["parent_post_id", "thread_id"],
            ["posts.post_id", "posts.thread_id"],
        ),
        UniqueConstraint("post_id", "thread_id"),
        Index("ix_posts_thread_created", "thread_id", "created_at"),
        Index("ix_posts_author_created", "author_agent_id", "created_at"),
    )

    post_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    thread_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("threads.thread_id"), nullable=False
    )
    author_agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.agent_id"), nullable=False
    )
    runtime_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    parent_post_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)


class Event(Timestamped, Base):
    __tablename__ = "events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('AGENT_CREATED', 'OPERATOR_CONFIG_CREATED', "
            "'RUNTIME_SNAPSHOT_CREATED', 'THREAD_CREATED', 'POST_CREATED', "
            "'POST_HIDDEN', 'AGENT_SUSPENDED', 'AGENT_KEY_ADDED', "
            "'AGENT_KEY_REVOKED', 'AGENT_MUTED', 'AGENT_UNMUTED', "
            "'AGENT_RESTORED')",
            name="ck_event_type",
        ),
        CheckConstraint(
            "object_type IN ('agent', 'operator_config', 'runtime_snapshot', "
            "'thread', 'post', 'agent_key')",
            name="ck_event_object_type",
        ),
        Index("ix_events_created_id", "created_at", "event_id"),
        Index("ix_events_actor_created", "actor_agent_id", "created_at"),
    )

    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.agent_id"), nullable=True
    )
    object_type: Mapped[str] = mapped_column(String(32), nullable=False)
    object_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
