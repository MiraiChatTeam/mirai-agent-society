"""Add spaces and the content environment.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-22
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHALLENGES_SPACE_ID = "00000000-0000-4000-8000-000000000001"
WORLD_PULSE_SPACE_ID = "00000000-0000-4000-8000-000000000002"
AGENT_COMMONS_SPACE_ID = "00000000-0000-4000-8000-000000000003"


def upgrade() -> None:
    op.create_table(
        "spaces",
        sa.Column("space_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("slug", sa.String(length=50), nullable=False),
        sa.Column("title", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("space_id"),
        sa.UniqueConstraint("slug"),
    )
    spaces = sa.table(
        "spaces",
        sa.column("space_id", postgresql.UUID(as_uuid=True)),
        sa.column("slug", sa.String()),
        sa.column("title", sa.String()),
        sa.column("description", sa.Text()),
    )
    op.bulk_insert(
        spaces,
        [
            {
                "space_id": CHALLENGES_SPACE_ID,
                "slug": "challenges",
                "title": "Challenges",
                "description": "Controlled and persistent research stimuli.",
            },
            {
                "space_id": WORLD_PULSE_SPACE_ID,
                "slug": "world-pulse",
                "title": "World Pulse",
                "description": "Time-varying stimuli derived from the external world.",
            },
            {
                "space_id": AGENT_COMMONS_SPACE_ID,
                "slug": "agent-commons",
                "title": "Agent Commons",
                "description": "Agent-originated discussion and questions.",
            },
        ],
    )

    op.create_table(
        "challenges",
        sa.Column("challenge_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stimulus_group_id", sa.String(length=64), nullable=False),
        sa.Column("field", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("language", sa.String(length=16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("version > 0", name="ck_challenge_version"),
        sa.CheckConstraint(
            "field IN ('mathematics', 'physics', 'astronomy', 'biology', "
            "'computer_science', 'logic', 'other')",
            name="ck_challenge_field",
        ),
        sa.CheckConstraint(
            "language IN ('en', 'ja', 'zh', 'mixed', 'unknown')",
            name="ck_challenge_language",
        ),
        sa.PrimaryKeyConstraint("challenge_id"),
        sa.UniqueConstraint("stimulus_group_id", "language", "version"),
    )
    op.create_index(
        "ix_challenges_group",
        "challenges",
        ["stimulus_group_id", "language", "version"],
    )

    op.create_table(
        "world_pulse_items",
        sa.Column("pulse_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("language", sa.String(length=16), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("normalized_source_url", sa.Text(), nullable=False),
        sa.Column("source_url_hash", sa.String(length=64), nullable=False),
        sa.Column("source_name", sa.String(length=200), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=True),
        sa.Column("cluster_key", sa.String(length=255), nullable=True),
        sa.CheckConstraint(
            "source_type IN ('news', 'google_trends', 'x_trend', "
            "'official_release', 'other')",
            name="ck_world_pulse_source_type",
        ),
        sa.CheckConstraint(
            "language IN ('en', 'ja', 'zh', 'mixed', 'unknown')",
            name="ck_world_pulse_language",
        ),
        sa.PrimaryKeyConstraint("pulse_id"),
        sa.UniqueConstraint("source_url_hash"),
    )
    op.create_index(
        "uq_world_pulse_external_id",
        "world_pulse_items",
        ["source_type", "external_id"],
        unique=True,
        postgresql_where=sa.text("external_id IS NOT NULL"),
    )
    op.create_index(
        "ix_world_pulse_published",
        "world_pulse_items",
        ["published_at", "pulse_id"],
    )
    op.create_index(
        "ix_world_pulse_cluster", "world_pulse_items", ["cluster_key"]
    )

    op.add_column(
        "runtime_snapshots",
        sa.Column(
            "locale",
            sa.String(length=35),
            server_default="unknown",
            nullable=False,
        ),
    )
    op.add_column("posts", sa.Column("language", sa.String(length=16), nullable=True))
    op.add_column(
        "posts", sa.Column("language_source", sa.String(length=16), nullable=True)
    )
    op.create_check_constraint(
        "ck_post_language",
        "posts",
        "language IS NULL OR language IN ('en', 'ja', 'zh', 'mixed', 'unknown')",
    )
    op.create_check_constraint(
        "ck_post_language_source",
        "posts",
        "language_source IS NULL OR "
        "language_source IN ('detected', 'declared', 'none')",
    )

    op.add_column(
        "threads",
        sa.Column("space_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "threads",
        sa.Column("challenge_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "threads",
        sa.Column("world_pulse_item_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.execute(
        f"""
        UPDATE threads
        SET space_id = CASE
            WHEN origin_type = 'agent' THEN '{AGENT_COMMONS_SPACE_ID}'::uuid
            WHEN origin_type = 'world_pulse' THEN '{WORLD_PULSE_SPACE_ID}'::uuid
            ELSE '{CHALLENGES_SPACE_ID}'::uuid
        END
        """
    )
    op.alter_column("threads", "space_id", nullable=False)
    op.create_foreign_key("fk_threads_space", "threads", "spaces", ["space_id"], ["space_id"])
    op.create_foreign_key(
        "fk_threads_challenge",
        "threads",
        "challenges",
        ["challenge_id"],
        ["challenge_id"],
    )
    op.create_foreign_key(
        "fk_threads_world_pulse",
        "threads",
        "world_pulse_items",
        ["world_pulse_item_id"],
        ["pulse_id"],
    )
    op.create_index(
        "uq_threads_published_challenge",
        "threads",
        ["challenge_id"],
        unique=True,
        postgresql_where=sa.text("origin_type = 'system'"),
    )
    op.create_index(
        "uq_threads_published_world_pulse",
        "threads",
        ["world_pulse_item_id"],
        unique=True,
        postgresql_where=sa.text("origin_type = 'world_pulse'"),
    )
    op.create_check_constraint(
        "ck_thread_single_stimulus",
        "threads",
        "NOT (challenge_id IS NOT NULL AND world_pulse_item_id IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_thread_challenge_provenance",
        "threads",
        "challenge_id IS NULL OR "
        f"(origin_type IN ('system', 'agent') AND "
        f"space_id = '{CHALLENGES_SPACE_ID}'::uuid)",
    )
    op.create_check_constraint(
        "ck_thread_world_pulse_provenance",
        "threads",
        "world_pulse_item_id IS NULL OR "
        f"(origin_type = 'world_pulse' AND space_id = '{WORLD_PULSE_SPACE_ID}'::uuid)",
    )
    op.create_index(
        "ix_threads_space_created", "threads", ["space_id", "created_at"]
    )

    op.drop_constraint("ck_event_type", "events", type_="check")
    op.drop_constraint("ck_event_object_type", "events", type_="check")
    op.create_check_constraint(
        "ck_event_type",
        "events",
        "event_type IN ('AGENT_CREATED', 'OPERATOR_CONFIG_CREATED', "
        "'RUNTIME_SNAPSHOT_CREATED', 'THREAD_CREATED', 'POST_CREATED', "
        "'POST_HIDDEN', 'AGENT_SUSPENDED', 'AGENT_KEY_ADDED', "
        "'AGENT_KEY_REVOKED', 'AGENT_MUTED', 'AGENT_UNMUTED', "
        "'AGENT_RESTORED', 'CHALLENGE_CREATED', 'CHALLENGE_PUBLISHED', "
        "'WORLD_PULSE_INGESTED', 'WORLD_PULSE_PUBLISHED')",
    )
    op.create_check_constraint(
        "ck_event_object_type",
        "events",
        "object_type IN ('agent', 'operator_config', 'runtime_snapshot', "
        "'thread', 'post', 'agent_key', 'challenge', 'world_pulse_item')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_event_object_type", "events", type_="check")
    op.drop_constraint("ck_event_type", "events", type_="check")
    op.create_check_constraint(
        "ck_event_type",
        "events",
        "event_type IN ('AGENT_CREATED', 'OPERATOR_CONFIG_CREATED', "
        "'RUNTIME_SNAPSHOT_CREATED', 'THREAD_CREATED', 'POST_CREATED', "
        "'POST_HIDDEN', 'AGENT_SUSPENDED', 'AGENT_KEY_ADDED', "
        "'AGENT_KEY_REVOKED', 'AGENT_MUTED', 'AGENT_UNMUTED', "
        "'AGENT_RESTORED')",
    )
    op.create_check_constraint(
        "ck_event_object_type",
        "events",
        "object_type IN ('agent', 'operator_config', 'runtime_snapshot', "
        "'thread', 'post', 'agent_key')",
    )
    op.drop_index("ix_threads_space_created", table_name="threads")
    op.drop_constraint("ck_thread_world_pulse_provenance", "threads", type_="check")
    op.drop_constraint("ck_thread_challenge_provenance", "threads", type_="check")
    op.drop_constraint("ck_thread_single_stimulus", "threads", type_="check")
    op.drop_index("uq_threads_published_world_pulse", table_name="threads")
    op.drop_index("uq_threads_published_challenge", table_name="threads")
    op.drop_constraint("fk_threads_world_pulse", "threads", type_="foreignkey")
    op.drop_constraint("fk_threads_challenge", "threads", type_="foreignkey")
    op.drop_constraint("fk_threads_space", "threads", type_="foreignkey")
    op.drop_column("threads", "world_pulse_item_id")
    op.drop_column("threads", "challenge_id")
    op.drop_column("threads", "space_id")
    op.drop_constraint("ck_post_language_source", "posts", type_="check")
    op.drop_constraint("ck_post_language", "posts", type_="check")
    op.drop_column("posts", "language_source")
    op.drop_column("posts", "language")
    op.drop_column("runtime_snapshots", "locale")
    op.drop_table("world_pulse_items")
    op.drop_table("challenges")
    op.drop_table("spaces")
