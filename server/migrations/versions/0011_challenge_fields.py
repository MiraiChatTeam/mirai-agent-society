"""Accept the reviewed M5 Challenge domains without collapsing them to other.

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-25
"""

from collections.abc import Sequence

from alembic import op


revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


OLD_FIELDS = (
    "'mathematics', 'physics', 'astronomy', 'biology', "
    "'computer_science', 'logic', 'other'"
)
NEW_FIELDS = (
    OLD_FIELDS + ", 'chemistry', 'climate_science', 'computational_linguistics', "
    "'earth_science', 'environmental_data_science', 'hydrology', "
    "'materials_science', 'ocean_science', 'structural_engineering'"
)


def upgrade() -> None:
    op.drop_constraint("ck_challenge_field", "challenges", type_="check")
    op.create_check_constraint(
        "ck_challenge_field", "challenges", f"field IN ({NEW_FIELDS})"
    )


def downgrade() -> None:
    op.drop_constraint("ck_challenge_field", "challenges", type_="check")
    op.create_check_constraint(
        "ck_challenge_field", "challenges", f"field IN ({OLD_FIELDS})"
    )
