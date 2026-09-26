"""Add optional, provenance-tagged World Pulse stimulus context.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-23
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("world_pulse_items", sa.Column("stimulus_summary", sa.Text(), nullable=True))
    op.add_column(
        "world_pulse_items",
        sa.Column(
            "summary_source", sa.String(length=32),
            server_default="unavailable", nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_world_pulse_summary_source",
        "world_pulse_items",
        "summary_source IN ('feed_metadata', 'publisher_page', 'trend_context', 'unavailable')",
    )
    op.create_check_constraint(
        "ck_world_pulse_stimulus_summary",
        "world_pulse_items",
        "(stimulus_summary IS NULL AND summary_source = 'unavailable') OR "
        "(stimulus_summary IS NOT NULL AND length(stimulus_summary) BETWEEN 1 AND 800 "
        "AND summary_source <> 'unavailable')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_world_pulse_stimulus_summary", "world_pulse_items", type_="check")
    op.drop_constraint("ck_world_pulse_summary_source", "world_pulse_items", type_="check")
    op.drop_column("world_pulse_items", "summary_source")
    op.drop_column("world_pulse_items", "stimulus_summary")
