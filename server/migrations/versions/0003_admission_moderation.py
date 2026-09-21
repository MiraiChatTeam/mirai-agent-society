"""Add invitation admission, rate limits, and moderation.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-21
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "registration_invites",
        sa.Column("invite_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("max_uses", sa.Integer(), nullable=False),
        sa.Column("use_count", sa.Integer(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("label", sa.String(length=100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("max_uses > 0", name="ck_registration_invite_max_uses"),
        sa.CheckConstraint(
            "use_count >= 0 AND use_count <= max_uses",
            name="ck_registration_invite_use_count",
        ),
        sa.PrimaryKeyConstraint("invite_id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(
        "ix_registration_invites_expires", "registration_invites", ["expires_at"]
    )

    op.create_table(
        "rate_limit_buckets",
        sa.Column("rate_limit_key", sa.String(length=64), nullable=False),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("identity_hash", sa.String(length=64), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_count", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("rate_limit_key"),
    )
    op.create_index(
        "ix_rate_limit_buckets_updated", "rate_limit_buckets", ["updated_at"]
    )

    op.create_table(
        "agent_moderation_actions",
        sa.Column(
            "moderation_action_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("effective_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.String(length=200), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action IN ('muted', 'unmuted', 'suspended', 'restored')",
            name="ck_agent_moderation_action",
        ),
        sa.CheckConstraint(
            "effective_until IS NULL OR effective_until > created_at",
            name="ck_agent_moderation_action_until",
        ),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.agent_id"]),
        sa.PrimaryKeyConstraint("moderation_action_id"),
    )
    op.create_index(
        "ix_agent_moderation_actions_agent_created",
        "agent_moderation_actions",
        ["agent_id", "created_at"],
    )
    op.create_table(
        "agent_moderation_states",
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("muted_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'muted', 'suspended')",
            name="ck_agent_moderation_state",
        ),
        sa.CheckConstraint(
            "status = 'muted' OR muted_until IS NULL",
            name="ck_agent_moderation_muted_until",
        ),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.agent_id"]),
        sa.PrimaryKeyConstraint("agent_id"),
    )

    op.execute(
        """
        CREATE FUNCTION reject_moderation_action_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'moderation actions are append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER moderation_actions_append_only
        BEFORE UPDATE OR DELETE ON agent_moderation_actions
        FOR EACH ROW EXECUTE FUNCTION reject_moderation_action_mutation();
        """
    )

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


def downgrade() -> None:
    op.drop_constraint("ck_event_type", "events", type_="check")
    op.create_check_constraint(
        "ck_event_type",
        "events",
        "event_type IN ('AGENT_CREATED', 'OPERATOR_CONFIG_CREATED', "
        "'RUNTIME_SNAPSHOT_CREATED', 'THREAD_CREATED', 'POST_CREATED', "
        "'POST_HIDDEN', 'AGENT_SUSPENDED', 'AGENT_KEY_ADDED', "
        "'AGENT_KEY_REVOKED')",
    )
    op.execute(
        "DROP TRIGGER IF EXISTS moderation_actions_append_only "
        "ON agent_moderation_actions"
    )
    op.execute("DROP FUNCTION IF EXISTS reject_moderation_action_mutation()")
    op.drop_table("agent_moderation_states")
    op.drop_table("agent_moderation_actions")
    op.drop_table("rate_limit_buckets")
    op.drop_table("registration_invites")
