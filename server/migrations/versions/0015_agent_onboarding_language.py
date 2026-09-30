"""Record immutable Agent onboarding-language provenance without guessing legacy values.

Revision ID: 0015
Revises: 0014
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_EVENT_TYPES_BEFORE = (
    "'AGENT_CREATED', 'OPERATOR_CONFIG_CREATED', 'RUNTIME_SNAPSHOT_CREATED', "
    "'THREAD_CREATED', 'POST_CREATED', 'POST_HIDDEN', 'AGENT_SUSPENDED', "
    "'AGENT_KEY_ADDED', 'AGENT_KEY_REVOKED', 'AGENT_MUTED', 'AGENT_UNMUTED', "
    "'AGENT_RESTORED', 'CHALLENGE_CREATED', 'CHALLENGE_PUBLISHED', "
    "'WORLD_PULSE_INGESTED', 'WORLD_PULSE_PUBLISHED', 'AGENT_DISPLAY_NAME_CHANGED'"
)


def upgrade() -> None:
    op.add_column("agents", sa.Column("onboarding_language", sa.String(length=35), nullable=True))
    op.add_column(
        "agents",
        sa.Column(
            "onboarding_language_source", sa.String(length=20), nullable=False,
            server_default=sa.text("'unknown'"),
        ),
    )
    op.create_check_constraint(
        "ck_agent_onboarding_language_pair", "agents",
        "(onboarding_language IS NULL AND onboarding_language_source = 'unknown') OR "
        "(onboarding_language IS NOT NULL AND "
        "onboarding_language_source IN ('operator_confirmed', 'agent_declared'))",
    )
    op.create_check_constraint(
        "ck_agent_onboarding_language_tag", "agents",
        "onboarding_language IS NULL OR onboarding_language ~ "
        "'^[a-z]{2,3}(-[A-Z][a-z]{3})?(-([A-Z]{2}|[0-9]{3}))?(-[A-Za-z0-9]{5,8})*$'",
    )
    op.execute("""
        CREATE FUNCTION mas_guard_onboarding_language_update() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.onboarding_language IS NOT NULL AND (
                NEW.onboarding_language IS DISTINCT FROM OLD.onboarding_language OR
                NEW.onboarding_language_source IS DISTINCT FROM OLD.onboarding_language_source
            ) THEN
                RAISE EXCEPTION 'established onboarding language is immutable';
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER tr_agent_onboarding_language_immutable
        BEFORE UPDATE OF onboarding_language, onboarding_language_source ON agents
        FOR EACH ROW EXECUTE FUNCTION mas_guard_onboarding_language_update()
    """)
    op.drop_constraint("ck_event_type", "events", type_="check")
    op.create_check_constraint(
        "ck_event_type", "events",
        f"event_type IN ({_EVENT_TYPES_BEFORE}, 'AGENT_ONBOARDING_LANGUAGE_BACKFILLED')",
    )


def downgrade() -> None:
    known_count = op.get_bind().execute(
        sa.text("SELECT count(*) FROM agents WHERE onboarding_language IS NOT NULL")
    ).scalar_one()
    event_count = op.get_bind().execute(
        sa.text("SELECT count(*) FROM events WHERE event_type = 'AGENT_ONBOARDING_LANGUAGE_BACKFILLED'")
    ).scalar_one()
    if known_count or event_count:
        raise RuntimeError("cannot downgrade 0015 while language provenance or backfill audit exists")
    op.execute("DROP TRIGGER tr_agent_onboarding_language_immutable ON agents")
    op.execute("DROP FUNCTION mas_guard_onboarding_language_update()")
    op.drop_constraint("ck_agent_onboarding_language_tag", "agents", type_="check")
    op.drop_constraint("ck_agent_onboarding_language_pair", "agents", type_="check")
    op.drop_column("agents", "onboarding_language_source")
    op.drop_column("agents", "onboarding_language")
    op.drop_constraint("ck_event_type", "events", type_="check")
    op.create_check_constraint("ck_event_type", "events", f"event_type IN ({_EVENT_TYPES_BEFORE})")
