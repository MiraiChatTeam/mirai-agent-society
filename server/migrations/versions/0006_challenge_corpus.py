"""Add Challenge types and normalized provenance.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-22
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "challenges",
        sa.Column("challenge_type", sa.String(length=16), nullable=True),
    )
    op.create_check_constraint(
        "ck_challenge_type",
        "challenges",
        "challenge_type IS NULL OR "
        "challenge_type IN ('verifiable', 'open', 'debatable')",
    )
    op.create_table(
        "challenge_sources",
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("challenge_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("source_name", sa.String(length=300), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("source_role", sa.String(length=32), nullable=False),
        sa.CheckConstraint(
            "source_kind IN ('generated', 'literature_anchored')",
            name="ck_challenge_source_kind",
        ),
        sa.CheckConstraint(
            "source_role IN ('task_design', 'background_anchor')",
            name="ck_challenge_source_role",
        ),
        sa.ForeignKeyConstraint(
            ["challenge_id"],
            ["challenges.challenge_id"],
            name="fk_challenge_source_challenge",
        ),
        sa.PrimaryKeyConstraint("source_id"),
        sa.UniqueConstraint(
            "challenge_id",
            "source_kind",
            "source_name",
            "source_role",
            name="uq_challenge_source_identity",
        ),
    )
    op.create_index(
        "ix_challenge_sources_challenge",
        "challenge_sources",
        ["challenge_id", "source_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_challenge_sources_challenge", table_name="challenge_sources")
    op.drop_table("challenge_sources")
    op.drop_constraint("ck_challenge_type", "challenges", type_="check")
    op.drop_column("challenges", "challenge_type")
