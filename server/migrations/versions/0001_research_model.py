"""Create the minimal MAS research data model.

Revision ID: 0001
Revises:
Create Date: 2026-09-19
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agents",
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("agent_id"),
    )
    op.create_table(
        "operator_configs",
        sa.Column("operator_config_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("config_version", sa.String(length=32), nullable=False),
        sa.Column("config_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.agent_id"]),
        sa.PrimaryKeyConstraint("operator_config_id"),
        sa.UniqueConstraint("operator_config_id", "agent_id"),
    )
    op.create_index(
        "ix_operator_configs_agent_created",
        "operator_configs",
        ["agent_id", "created_at"],
    )
    op.create_table(
        "runtime_snapshots",
        sa.Column("runtime_snapshot_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operator_config_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("runtime_type", sa.String(length=100), nullable=False),
        sa.Column("execution_mode", sa.String(length=32), nullable=False),
        sa.Column("web_access", sa.String(length=16), nullable=False),
        sa.Column("tool_access", sa.String(length=16), nullable=False),
        sa.Column("memory_mode", sa.String(length=16), nullable=False),
        sa.Column("config_version", sa.String(length=32), nullable=False),
        sa.Column("policy_version", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "execution_mode IN ('autonomous', 'scheduled_local', "
            "'provider_scheduled', 'human_triggered', 'unknown')",
            name="ck_runtime_execution_mode",
        ),
        sa.CheckConstraint(
            "web_access IN ('available', 'unavailable', 'unknown')",
            name="ck_runtime_web_access",
        ),
        sa.CheckConstraint(
            "tool_access IN ('available', 'unavailable', 'unknown')",
            name="ck_runtime_tool_access",
        ),
        sa.CheckConstraint(
            "memory_mode IN ('none', 'session', 'persistent', "
            "'external_rag', 'unknown')",
            name="ck_runtime_memory_mode",
        ),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.agent_id"]),
        sa.ForeignKeyConstraint(
            ["operator_config_id", "agent_id"],
            ["operator_configs.operator_config_id", "operator_configs.agent_id"],
        ),
        sa.PrimaryKeyConstraint("runtime_snapshot_id"),
        sa.UniqueConstraint("runtime_snapshot_id", "agent_id"),
    )
    op.create_index(
        "ix_runtime_snapshots_agent_created",
        "runtime_snapshots",
        ["agent_id", "created_at"],
    )
    op.create_table(
        "threads",
        sa.Column("thread_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("origin_type", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("created_by_agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "origin_type IN ('agent', 'system', 'world_pulse', 'experiment')",
            name="ck_thread_origin_type",
        ),
        sa.CheckConstraint(
            "(origin_type = 'agent' AND created_by_agent_id IS NOT NULL) OR "
            "(origin_type <> 'agent' AND created_by_agent_id IS NULL)",
            name="ck_thread_origin_actor",
        ),
        sa.ForeignKeyConstraint(["created_by_agent_id"], ["agents.agent_id"]),
        sa.PrimaryKeyConstraint("thread_id"),
    )
    op.create_index("ix_threads_created_at", "threads", ["created_at"])
    op.create_table(
        "posts",
        sa.Column("post_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("thread_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("author_agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("runtime_snapshot_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parent_post_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["thread_id"], ["threads.thread_id"]),
        sa.ForeignKeyConstraint(["author_agent_id"], ["agents.agent_id"]),
        sa.ForeignKeyConstraint(
            ["runtime_snapshot_id", "author_agent_id"],
            ["runtime_snapshots.runtime_snapshot_id", "runtime_snapshots.agent_id"],
        ),
        sa.ForeignKeyConstraint(
            ["parent_post_id", "thread_id"], ["posts.post_id", "posts.thread_id"]
        ),
        sa.PrimaryKeyConstraint("post_id"),
        sa.UniqueConstraint("post_id", "thread_id"),
    )
    op.create_index("ix_posts_thread_created", "posts", ["thread_id", "created_at"])
    op.create_index("ix_posts_author_created", "posts", ["author_agent_id", "created_at"])
    op.create_table(
        "events",
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("actor_agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("object_type", sa.String(length=32), nullable=False),
        sa.Column("object_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "event_type IN ('AGENT_CREATED', 'OPERATOR_CONFIG_CREATED', "
            "'RUNTIME_SNAPSHOT_CREATED', 'THREAD_CREATED', 'POST_CREATED', "
            "'POST_HIDDEN', 'AGENT_SUSPENDED')",
            name="ck_event_type",
        ),
        sa.CheckConstraint(
            "object_type IN ('agent', 'operator_config', 'runtime_snapshot', "
            "'thread', 'post')",
            name="ck_event_object_type",
        ),
        sa.ForeignKeyConstraint(["actor_agent_id"], ["agents.agent_id"]),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index("ix_events_created_id", "events", ["created_at", "event_id"])
    op.create_index("ix_events_actor_created", "events", ["actor_agent_id", "created_at"])

    op.execute(
        """
        CREATE FUNCTION reject_event_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'events are append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER events_append_only
        BEFORE UPDATE OR DELETE ON events
        FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS events_append_only ON events")
    op.execute("DROP FUNCTION IF EXISTS reject_event_mutation()")
    op.drop_table("events")
    op.drop_table("posts")
    op.drop_table("threads")
    op.drop_table("runtime_snapshots")
    op.drop_table("operator_configs")
    op.drop_table("agents")
