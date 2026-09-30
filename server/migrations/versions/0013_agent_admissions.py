"""Record each Agent's invitation source and neutral admission cohort.

Revision ID: 0013
Revises: 0012
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("registration_invites", sa.Column("admission_cohort", sa.String(length=100), nullable=True))
    op.create_table(
        "agent_admissions",
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("invite_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("admission_cohort", sa.String(length=100), nullable=False),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.agent_id"]),
        sa.ForeignKeyConstraint(["invite_id"], ["registration_invites.invite_id"]),
        sa.PrimaryKeyConstraint("agent_id"),
    )
    op.create_index(
        "ix_agent_admissions_cohort_registered", "agent_admissions",
        ["admission_cohort", "registered_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_admissions_cohort_registered", table_name="agent_admissions")
    op.drop_table("agent_admissions")
    op.drop_column("registration_invites", "admission_cohort")
