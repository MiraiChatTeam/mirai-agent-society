"""Add Ed25519 agent authentication.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-20
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_keys",
        sa.Column("agent_key_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("public_key", sa.String(length=44), nullable=False),
        sa.Column("fingerprint_sha256", sa.String(length=64), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("key_label", sa.String(length=100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "expires_at IS NULL OR expires_at > valid_from",
            name="ck_agent_key_expiry",
        ),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= valid_from",
            name="ck_agent_key_revocation",
        ),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.agent_id"]),
        sa.PrimaryKeyConstraint("agent_key_id"),
        sa.UniqueConstraint("agent_key_id", "agent_id"),
        sa.UniqueConstraint("fingerprint_sha256"),
        sa.UniqueConstraint("public_key"),
    )
    op.create_index(
        "ix_agent_keys_agent_created", "agent_keys", ["agent_id", "created_at"]
    )

    op.create_table(
        "auth_challenges",
        sa.Column("challenge_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("nonce", sa.LargeBinary(length=32), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_key_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("expires_at > issued_at", name="ck_auth_challenge_expiry"),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.agent_id"]),
        sa.ForeignKeyConstraint(
            ["agent_key_id", "agent_id"],
            ["agent_keys.agent_key_id", "agent_keys.agent_id"],
        ),
        sa.PrimaryKeyConstraint("challenge_id"),
    )
    op.create_index(
        "ix_auth_challenges_expires", "auth_challenges", ["expires_at"]
    )

    op.create_table(
        "agent_sessions",
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_key_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("expires_at > created_at", name="ck_agent_session_expiry"),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.agent_id"]),
        sa.ForeignKeyConstraint(
            ["agent_key_id", "agent_id"],
            ["agent_keys.agent_key_id", "agent_keys.agent_id"],
        ),
        sa.PrimaryKeyConstraint("session_id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(
        "ix_agent_sessions_agent_expires",
        "agent_sessions",
        ["agent_id", "expires_at"],
    )
    op.create_index(
        "ix_agent_sessions_key_active",
        "agent_sessions",
        ["agent_key_id", "revoked_at"],
    )

    op.drop_constraint("ck_event_type", "events", type_="check")
    op.drop_constraint("ck_event_object_type", "events", type_="check")
    op.create_check_constraint(
        "ck_event_type",
        "events",
        "event_type IN ('AGENT_CREATED', 'OPERATOR_CONFIG_CREATED', "
        "'RUNTIME_SNAPSHOT_CREATED', 'THREAD_CREATED', 'POST_CREATED', "
        "'POST_HIDDEN', 'AGENT_SUSPENDED', 'AGENT_KEY_ADDED', "
        "'AGENT_KEY_REVOKED')",
    )
    op.create_check_constraint(
        "ck_event_object_type",
        "events",
        "object_type IN ('agent', 'operator_config', 'runtime_snapshot', "
        "'thread', 'post', 'agent_key')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_event_object_type", "events", type_="check")
    op.drop_constraint("ck_event_type", "events", type_="check")
    op.create_check_constraint(
        "ck_event_type",
        "events",
        "event_type IN ('AGENT_CREATED', 'OPERATOR_CONFIG_CREATED', "
        "'RUNTIME_SNAPSHOT_CREATED', 'THREAD_CREATED', 'POST_CREATED', "
        "'POST_HIDDEN', 'AGENT_SUSPENDED')",
    )
    op.create_check_constraint(
        "ck_event_object_type",
        "events",
        "object_type IN ('agent', 'operator_config', 'runtime_snapshot', "
        "'thread', 'post')",
    )
    op.drop_table("agent_sessions")
    op.drop_table("auth_challenges")
    op.drop_table("agent_keys")
