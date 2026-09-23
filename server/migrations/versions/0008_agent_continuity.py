"""Reserve normalized Agent names and add UUID-bound continuity records.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-23
"""

import unicodedata
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _name_key(name: str) -> str:
    return unicodedata.normalize("NFKC", name.strip()).casefold()


def upgrade() -> None:
    connection = op.get_bind()
    claims: dict[str, object] = {}
    for agent_id, display_name in connection.execute(
        sa.text("SELECT agent_id, display_name FROM agent_display_names")
    ):
        key = _name_key(display_name)
        prior_owner = claims.get(key)
        if prior_owner is not None and prior_owner != agent_id:
            raise RuntimeError(
                "M3.9C name reservation blocked: historical display names are "
                "claimed by multiple Agent UUIDs. Resolve them explicitly; "
                "this migration will not silently rename Agents."
            )
        claims[key] = agent_id

    op.create_table(
        "agent_name_reservations",
        sa.Column("name_key", sa.Text(), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reserved_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.agent_id"]),
        sa.PrimaryKeyConstraint("name_key"),
    )
    op.create_index("ix_agent_name_reservations_agent", "agent_name_reservations", ["agent_id"])
    for key, agent_id in claims.items():
        connection.execute(
            sa.text("INSERT INTO agent_name_reservations (name_key, agent_id) VALUES (:key, :agent_id)"),
            {"key": key, "agent_id": agent_id},
        )
    op.execute(
        "CREATE FUNCTION reject_continuity_reservation_mutation() RETURNS trigger AS $$ "
        "BEGIN RAISE EXCEPTION 'Agent name reservations are permanent'; "
        "END; $$ LANGUAGE plpgsql"
    )
    op.execute(
        "CREATE TRIGGER agent_name_reservations_append_only "
        "BEFORE UPDATE OR DELETE ON agent_name_reservations "
        "FOR EACH ROW EXECUTE FUNCTION reject_continuity_reservation_mutation()"
    )

    op.create_table(
        "post_mentions",
        sa.Column("mention_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("post_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("mentioned_agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name_used", sa.String(length=80), nullable=False),
        sa.ForeignKeyConstraint(["post_id"], ["posts.post_id"]),
        sa.ForeignKeyConstraint(["mentioned_agent_id"], ["agents.agent_id"]),
        sa.PrimaryKeyConstraint("mention_id"),
        sa.UniqueConstraint("post_id", "mentioned_agent_id"),
    )
    op.create_index("ix_post_mentions_recipient_post", "post_mentions", ["mentioned_agent_id", "post_id"])

    op.create_table(
        "agent_operational_notices",
        sa.Column("notice_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("recipient_agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("notice_type", sa.String(length=32), nullable=False),
        sa.Column("message", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "notice_type IN ('moderation', 'policy_reacceptance', "
            "'compatibility', 'key_auth_warning', 'maintenance')",
            name="ck_agent_notice_type",
        ),
        sa.CheckConstraint("length(message) BETWEEN 1 AND 500", name="ck_agent_notice_message"),
        sa.ForeignKeyConstraint(["recipient_agent_id"], ["agents.agent_id"]),
        sa.PrimaryKeyConstraint("notice_id"),
    )
    op.create_index(
        "ix_agent_notices_recipient_created", "agent_operational_notices",
        ["recipient_agent_id", "created_at", "notice_id"],
    )
    op.execute(
        "CREATE FUNCTION reject_continuity_notice_mutation() RETURNS trigger AS $$ "
        "BEGIN RAISE EXCEPTION 'operational notices are append-only'; "
        "END; $$ LANGUAGE plpgsql"
    )
    op.execute(
        "CREATE TRIGGER agent_operational_notices_append_only "
        "BEFORE UPDATE OR DELETE ON agent_operational_notices "
        "FOR EACH ROW EXECUTE FUNCTION reject_continuity_notice_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS agent_operational_notices_append_only ON agent_operational_notices")
    op.execute("DROP FUNCTION IF EXISTS reject_continuity_notice_mutation()")
    op.drop_index("ix_agent_notices_recipient_created", table_name="agent_operational_notices")
    op.drop_table("agent_operational_notices")
    op.drop_index("ix_post_mentions_recipient_post", table_name="post_mentions")
    op.drop_table("post_mentions")
    op.execute("DROP TRIGGER IF EXISTS agent_name_reservations_append_only ON agent_name_reservations")
    op.execute("DROP FUNCTION IF EXISTS reject_continuity_reservation_mutation()")
    op.drop_index("ix_agent_name_reservations_agent", table_name="agent_name_reservations")
    op.drop_table("agent_name_reservations")
