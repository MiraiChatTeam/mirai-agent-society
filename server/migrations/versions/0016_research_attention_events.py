"""Add private append-only research attention events.

Revision ID: 0016
Revises: 0015
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "research_attention_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agents.agent_id"), nullable=False),
        sa.Column("runtime_snapshot_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("runtime_snapshots.runtime_snapshot_id"), nullable=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("server_received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.UniqueConstraint("agent_id", "event_id", name="uq_research_attention_agent_event"),
        sa.CheckConstraint("schema_version = 1", name="ck_research_attention_schema_version"),
        sa.CheckConstraint(
            "event_type IN ('source_fetched', 'thread_opened', 'source_handled', 'run_outcome')",
            name="ck_research_attention_event_type",
        ),
    )
    op.create_index("ix_research_attention_agent_run", "research_attention_events", ["agent_id", "run_id", "occurred_at"])
    op.create_index("ix_research_attention_type_received", "research_attention_events", ["event_type", "server_received_at"])
    op.execute("""
        CREATE FUNCTION mas_research_attention_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'research attention events are append-only';
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER tr_research_attention_immutable
        BEFORE UPDATE OR DELETE ON research_attention_events
        FOR EACH ROW EXECUTE FUNCTION mas_research_attention_immutable()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER tr_research_attention_immutable ON research_attention_events")
    op.execute("DROP FUNCTION mas_research_attention_immutable()")
    op.drop_index("ix_research_attention_type_received", table_name="research_attention_events")
    op.drop_index("ix_research_attention_agent_run", table_name="research_attention_events")
    op.drop_table("research_attention_events")
