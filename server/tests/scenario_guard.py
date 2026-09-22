"""Safety guard for acceptance scenarios that intentionally persist fixtures."""

import os

from sqlalchemy.engine import make_url


def require_isolated_test_environment() -> None:
    """Refuse to run fixture-writing scenarios against the MAS primary database."""
    if os.getenv("MAS_TESTING") != "1":
        raise RuntimeError(
            "Acceptance scenarios require MAS_TESTING=1 and the isolated test stack. "
            "Run scripts/test-isolated.sh instead."
        )

    database_url = make_url(os.environ["DATABASE_URL"])
    if database_url.database != "mas_test":
        raise RuntimeError(
            f"Refusing fixture writes to database {database_url.database!r}; "
            "the required database name is 'mas_test'."
        )
