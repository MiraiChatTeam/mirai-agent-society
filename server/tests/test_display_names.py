import unittest
from datetime import UTC, datetime, timedelta

from app.display_names import count_renames_in_window, validate_display_name


class DisplayNameTests(unittest.TestCase):
    def test_unicode_trim_and_control_validation(self):
        self.assertEqual(validate_display_name("  ミライ・七  "), "ミライ・七")
        with self.assertRaises(ValueError):
            validate_display_name("   ")
        with self.assertRaises(ValueError):
            validate_display_name("visible\nspoof")
        with self.assertRaises(ValueError):
            validate_display_name("a\u200bb")

    def test_rolling_window_expires_at_thirty_days(self):
        now = datetime(2026, 9, 22, tzinfo=UTC)
        history = [
            now - timedelta(days=30),
            now - timedelta(days=29, hours=23),
            now - timedelta(days=1),
            now + timedelta(seconds=1),
        ]
        self.assertEqual(count_renames_in_window(history, now), 2)


if __name__ == "__main__":
    unittest.main()
