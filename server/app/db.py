import os

import psycopg


def check_database() -> bool:
    """Return True when PostgreSQL accepts a simple query."""
    database_url = os.environ["DATABASE_URL"]

    with psycopg.connect(database_url, connect_timeout=3) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            return cursor.fetchone() == (1,)
