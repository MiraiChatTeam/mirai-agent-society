"""Add World Pulse acquisition selection provenance.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-22
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "world_pulse_acquisitions",
        sa.Column("acquisition_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("pulse_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_adapter", sa.String(length=100), nullable=False),
        sa.Column("source_profile", sa.String(length=50), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("selection_date", sa.Date(), nullable=False),
        sa.Column("source_rank", sa.Integer(), nullable=True),
        sa.Column("selection_score", sa.Integer(), nullable=False),
        sa.Column("score_components", postgresql.JSONB(), nullable=False),
        sa.Column("collector_version", sa.String(length=32), nullable=False),
        sa.CheckConstraint("source_rank IS NULL OR source_rank > 0", name="ck_acquisition_rank"),
        sa.CheckConstraint("selection_score >= 0", name="ck_acquisition_score"),
        sa.ForeignKeyConstraint(
            ["pulse_id"], ["world_pulse_items.pulse_id"], name="fk_acquisition_pulse"
        ),
        sa.PrimaryKeyConstraint("acquisition_id"),
        sa.UniqueConstraint("pulse_id"),
    )
    op.create_index(
        "ix_acquisitions_selection_date",
        "world_pulse_acquisitions",
        ["selection_date", "acquisition_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_acquisitions_selection_date",
        table_name="world_pulse_acquisitions",
    )
    op.drop_table("world_pulse_acquisitions")
