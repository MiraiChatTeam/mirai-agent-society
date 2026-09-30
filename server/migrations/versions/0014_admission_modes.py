"""Add explicit private, public-cohort, and open admission modes.

Revision ID: 0014
Revises: 0013
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "registration_invites",
        sa.Column("admission_mode", sa.String(length=20), nullable=False,
                  server_default=sa.text("'private_invite'")),
    )
    op.add_column(
        "registration_invites",
        sa.Column("public_code", sa.String(length=100), nullable=True),
    )
    op.create_unique_constraint(
        "uq_registration_invites_public_code", "registration_invites", ["public_code"]
    )
    op.create_check_constraint(
        "ck_registration_invite_mode", "registration_invites",
        "admission_mode IN ('private_invite', 'public_cohort')",
    )
    op.create_check_constraint(
        "ck_registration_invite_public_code", "registration_invites",
        "(admission_mode = 'private_invite' AND public_code IS NULL) OR "
        "(admission_mode = 'public_cohort' AND public_code IS NOT NULL)",
    )
    op.alter_column("registration_invites", "admission_mode", server_default=None)

    op.add_column(
        "agent_admissions",
        sa.Column("admission_mode", sa.String(length=20), nullable=False,
                  server_default=sa.text("'private_invite'")),
    )
    op.alter_column("agent_admissions", "invite_id", nullable=True)
    op.create_check_constraint(
        "ck_agent_admission_mode", "agent_admissions",
        "admission_mode IN ('private_invite', 'public_cohort', 'open')",
    )
    op.create_check_constraint(
        "ck_agent_admission_source", "agent_admissions",
        "(admission_mode = 'open' AND invite_id IS NULL) OR "
        "(admission_mode IN ('private_invite', 'public_cohort') AND invite_id IS NOT NULL)",
    )
    op.alter_column("agent_admissions", "admission_mode", server_default=None)


def downgrade() -> None:
    op.drop_constraint("ck_agent_admission_source", "agent_admissions", type_="check")
    op.drop_constraint("ck_agent_admission_mode", "agent_admissions", type_="check")
    open_count = op.get_bind().execute(
        sa.text("SELECT count(*) FROM agent_admissions WHERE invite_id IS NULL")
    ).scalar_one()
    if open_count:
        raise RuntimeError("cannot downgrade 0014 while open admissions exist")
    op.alter_column("agent_admissions", "invite_id", nullable=False)
    op.drop_column("agent_admissions", "admission_mode")
    op.drop_constraint("ck_registration_invite_public_code", "registration_invites", type_="check")
    op.drop_constraint("ck_registration_invite_mode", "registration_invites", type_="check")
    op.drop_constraint("uq_registration_invites_public_code", "registration_invites", type_="unique")
    op.drop_column("registration_invites", "public_code")
    op.drop_column("registration_invites", "admission_mode")
