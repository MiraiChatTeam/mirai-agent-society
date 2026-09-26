"""Separate identity-recovery challenges from ordinary authentication.

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-25
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "auth_challenges",
        sa.Column(
            "purpose", sa.String(length=16), nullable=False,
            server_default=sa.text("'auth'"),
        ),
    )
    op.create_check_constraint(
        "ck_auth_challenge_purpose",
        "auth_challenges",
        "purpose IN ('auth', 'recovery')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_auth_challenge_purpose", "auth_challenges", type_="check")
    op.drop_column("auth_challenges", "purpose")
