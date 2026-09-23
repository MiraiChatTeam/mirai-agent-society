import hashlib
import json
import unittest
from pathlib import Path

from client.mas_client.control_plane import CONSTITUTION_SHA256


class ConstitutionBindingTests(unittest.TestCase):
    def test_control_digest_matches_canonical_constitution(self):
        source = Path(__file__).resolve().parents[2] / "docs" / "mas_constitution_v1.json"
        document = json.loads(source.read_text(encoding="utf-8"))
        canonical = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.assertEqual(hashlib.sha256(canonical).hexdigest(), CONSTITUTION_SHA256)


if __name__ == "__main__":
    unittest.main()
