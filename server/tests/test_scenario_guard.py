import os
import unittest
from unittest.mock import patch

from tests.scenario_guard import require_isolated_test_environment


class ScenarioGuardTests(unittest.TestCase):
    def test_primary_database_is_rejected(self) -> None:
        environment = {
            "MAS_TESTING": "1",
            "DATABASE_URL": "postgresql+psycopg://mas:secret@db:5432/mas",
        }
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(RuntimeError, "required database name"):
                require_isolated_test_environment()

    def test_missing_test_flag_is_rejected(self) -> None:
        environment = {
            "DATABASE_URL": "postgresql+psycopg://mas_test:secret@test-db:5432/mas_test",
        }
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(RuntimeError, "MAS_TESTING=1"):
                require_isolated_test_environment()

    def test_isolated_database_is_accepted(self) -> None:
        environment = {
            "MAS_TESTING": "1",
            "DATABASE_URL": "postgresql+psycopg://mas_test:secret@test-db:5432/mas_test",
        }
        with patch.dict(os.environ, environment, clear=True):
            require_isolated_test_environment()


if __name__ == "__main__":
    unittest.main()
