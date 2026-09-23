import uuid
import unittest
from datetime import UTC, datetime

from fastapi import HTTPException

from app.continuity import decode_cursor, encode_cursor
from app.display_names import normalized_name_key
from app.mentions import MENTION_PATTERN


class ContinuityUnitTests(unittest.TestCase):
    def test_normalized_names_preserve_display_but_compare_uniquely(self):
        self.assertEqual(normalized_name_key("  Ａｔｌａｓ  "), normalized_name_key("atlas"))
        self.assertEqual(normalized_name_key("Straße"), normalized_name_key("STRASSE"))

    def test_plain_and_braced_mentions_do_not_consume_sentence_punctuation(self):
        text = "Hello @AgentOne. And @{Agent Two}, but not mail@example.com."
        names = [match.group(1) or match.group(2) for match in MENTION_PATTERN.finditer(text)]
        self.assertEqual(names, ["AgentOne", "Agent Two"])

    def test_scope_tagged_cursor_roundtrip_and_rejects_cross_scope(self):
        identifier = uuid.uuid4()
        when = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
        encoded = encode_cursor("inbox", when, "reply", identifier)
        self.assertEqual(decode_cursor(encoded, "inbox"), (when, "reply", identifier))
        with self.assertRaises(HTTPException):
            decode_cursor(encoded, "posts")


if __name__ == "__main__":
    unittest.main()
