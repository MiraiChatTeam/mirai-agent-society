"""Add public display-name history and presentation summaries.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-22
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_display_names",
        sa.Column("display_name_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("display_name", sa.String(length=80), nullable=False),
        sa.Column("is_rename", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "display_name = btrim(display_name) AND length(display_name) > 0",
            name="ck_agent_display_name_nonempty",
        ),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.agent_id"]),
        sa.PrimaryKeyConstraint("display_name_id"),
        sa.UniqueConstraint("display_name_id", "agent_id"),
    )
    op.create_index(
        "ix_agent_display_names_agent_created",
        "agent_display_names",
        ["agent_id", "created_at"],
    )
    op.execute(
        """
        INSERT INTO agent_display_names
            (display_name_id, agent_id, display_name, is_rename, created_at)
        SELECT gen_random_uuid(), agent_id, 'Legacy Agent', false, created_at
        FROM agents
        """
    )
    op.execute(
        """
        CREATE FUNCTION reject_display_name_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'display-name history is append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER display_names_append_only
        BEFORE UPDATE OR DELETE ON agent_display_names
        FOR EACH ROW EXECUTE FUNCTION reject_display_name_mutation();
        """
    )

    op.add_column(
        "posts",
        sa.Column("display_name_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.execute(
        """
        UPDATE posts AS p
        SET display_name_id = n.display_name_id
        FROM agent_display_names AS n
        WHERE n.agent_id = p.author_agent_id AND n.is_rename = false
        """
    )
    op.alter_column("posts", "display_name_id", nullable=False)
    op.create_foreign_key(
        "fk_post_display_name_author",
        "posts",
        "agent_display_names",
        ["display_name_id", "author_agent_id"],
        ["display_name_id", "agent_id"],
    )

    op.add_column(
        "challenges", sa.Column("display_summary", sa.String(length=300), nullable=True)
    )
    op.execute("UPDATE challenges SET display_summary = title")
    op.alter_column("challenges", "display_summary", nullable=False)
    op.add_column(
        "world_pulse_items",
        sa.Column("display_summary", sa.String(length=300), nullable=True),
    )
    op.execute(
        """
        UPDATE world_pulse_items
        SET display_summary = left(
            array_to_string((regexp_split_to_array(btrim(summary), '\\s+'))[1:20], ' '),
            300
        )
        """
    )
    op.alter_column("world_pulse_items", "display_summary", nullable=False)

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
        "'WORLD_PULSE_INGESTED', 'WORLD_PULSE_PUBLISHED', "
        "'AGENT_DISPLAY_NAME_CHANGED')",
    )
    op.create_check_constraint(
        "ck_event_object_type",
        "events",
        "object_type IN ('agent', 'operator_config', 'runtime_snapshot', "
        "'thread', 'post', 'agent_key', 'challenge', 'world_pulse_item', "
        "'agent_display_name')",
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
        "'AGENT_RESTORED', 'CHALLENGE_CREATED', 'CHALLENGE_PUBLISHED', "
        "'WORLD_PULSE_INGESTED', 'WORLD_PULSE_PUBLISHED')",
    )
    op.create_check_constraint(
        "ck_event_object_type",
        "events",
        "object_type IN ('agent', 'operator_config', 'runtime_snapshot', "
        "'thread', 'post', 'agent_key', 'challenge', 'world_pulse_item')",
    )
    op.drop_column("world_pulse_items", "display_summary")
    op.drop_column("challenges", "display_summary")
    op.drop_constraint("fk_post_display_name_author", "posts", type_="foreignkey")
    op.drop_column("posts", "display_name_id")
    op.execute("DROP TRIGGER IF EXISTS display_names_append_only ON agent_display_names")
    op.execute("DROP FUNCTION IF EXISTS reject_display_name_mutation()")
    op.drop_index(
        "ix_agent_display_names_agent_created", table_name="agent_display_names"
    )
    op.drop_table("agent_display_names")
