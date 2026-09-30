"""Exercise fresh head and 0014 -> 0015 -> 0016 on an ephemeral database."""

import subprocess
import uuid

from sqlalchemy import text

from app.db import SessionLocal
from tests.scenario_guard import require_isolated_test_environment


def main() -> None:
    require_isolated_test_environment()
    # The test API starts from an empty database and automatically applies head.
    heads = subprocess.run(["alembic", "heads"], check=True, capture_output=True, text=True)
    assert heads.stdout.strip() == "0016 (head)", heads.stdout
    current = subprocess.run(["alembic", "current"], check=True, capture_output=True, text=True)
    assert current.stdout.strip() == "0016 (head)", current.stdout
    subprocess.run(["alembic", "downgrade", "0014"], check=True)
    agent_id = uuid.uuid4()
    with SessionLocal.begin() as db:
        db.execute(text("INSERT INTO agents (agent_id) VALUES (:id)"), {"id": agent_id})
    subprocess.run(["alembic", "upgrade", "head"], check=True)
    with SessionLocal.begin() as db:
        row = db.execute(text(
            "SELECT onboarding_language, onboarding_language_source FROM agents WHERE agent_id=:id"
        ), {"id": agent_id}).one()
        assert row == (None, "unknown"), row
        db.execute(text("DELETE FROM agents WHERE agent_id=:id"), {"id": agent_id})
    subprocess.run(["alembic", "downgrade", "0015"], check=True)
    subprocess.run(["alembic", "upgrade", "head"], check=True)
    subprocess.run(["alembic", "check"], check=True)
    print("Isolated fresh-head, 0014->0015->0016 and 0015->0016 migrations passed")


if __name__ == "__main__":
    main()
