import unittest
import uuid
from datetime import UTC, datetime

from fastapi import HTTPException

from app.content import normalize_source_url
from app.feed import decode_feed_cursor, encode_feed_cursor
from app.schemas import RuntimeSnapshotCreate


class SourceUrlTests(unittest.TestCase):
    def test_normalization_is_deterministic(self) -> None:
        self.assertEqual(
            normalize_source_url(
                "HTTPS://Example.COM:443/news/?b=2&a=1#ignored-fragment"
            ),
            "https://example.com/news?a=1&b=2",
        )

    def test_credentials_and_non_http_urls_are_rejected(self) -> None:
        for value in ("ftp://example.com/item", "https://user@example.com/item"):
            with self.assertRaises(ValueError):
                normalize_source_url(value)


class FeedCursorTests(unittest.TestCase):
    def test_cursor_round_trip(self) -> None:
        timestamp = datetime(2026, 9, 22, 1, 2, 3, 456789, tzinfo=UTC)
        thread_id = uuid.uuid4()
        self.assertEqual(
            decode_feed_cursor(encode_feed_cursor(timestamp, thread_id)),
            (timestamp, thread_id),
        )

    def test_invalid_cursor_is_rejected(self) -> None:
        with self.assertRaises(HTTPException):
            decode_feed_cursor("not-a-valid-cursor")


class LocaleTests(unittest.TestCase):
    def test_locale_defaults_to_unknown(self) -> None:
        snapshot = RuntimeSnapshotCreate(
            operator_config_id=uuid.uuid4(),
            config_version="0.4",
            policy_version="0.1",
        )
        self.assertEqual(snapshot.locale, "unknown")

    def test_locale_accepts_reported_bcp47_shape(self) -> None:
        snapshot = RuntimeSnapshotCreate(
            operator_config_id=uuid.uuid4(),
            config_version="0.4",
            policy_version="0.1",
            locale="ja-JP",
        )
        self.assertEqual(snapshot.locale, "ja-JP")

    def test_locale_rejects_guessed_free_text(self) -> None:
        with self.assertRaises(ValueError):
            RuntimeSnapshotCreate(
                operator_config_id=uuid.uuid4(),
                config_version="0.4",
                policy_version="0.1",
                locale="probably Japan",
            )
