"""Add source-preserving World Pulse event relations and verification status.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-23
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "world_pulse_items",
        sa.Column("verification_status", sa.String(length=32),
                  server_default="source_report_unverified", nullable=False),
    )
    op.execute(
        "UPDATE world_pulse_items SET verification_status = 'attention_signal' "
        "WHERE source_type = 'google_trends'"
    )
    op.create_check_constraint(
        "ck_world_pulse_verification_status", "world_pulse_items",
        "verification_status IN ('source_report_unverified', 'attention_signal')",
    )
    op.create_table(
        "world_pulse_event_relations",
        sa.Column("left_pulse_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("right_pulse_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("relation_type", sa.String(length=16), nullable=False),
        sa.Column("evidence_code", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(["left_pulse_id"], ["world_pulse_items.pulse_id"],
                                name="fk_wp_relation_left", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["right_pulse_id"], ["world_pulse_items.pulse_id"],
                                name="fk_wp_relation_right", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("left_pulse_id", "right_pulse_id"),
        sa.CheckConstraint("left_pulse_id < right_pulse_id", name="ck_wp_relation_order"),
        sa.CheckConstraint("relation_type IN ('same_event', 'follow_up', 'same_topic')",
                           name="ck_wp_relation_type"),
    )
    op.create_index("ix_wp_relation_right", "world_pulse_event_relations", ["right_pulse_id"])


def downgrade() -> None:
    op.drop_index("ix_wp_relation_right", table_name="world_pulse_event_relations")
    op.drop_table("world_pulse_event_relations")
    op.drop_constraint("ck_world_pulse_verification_status", "world_pulse_items", type_="check")
    op.drop_column("world_pulse_items", "verification_status")
