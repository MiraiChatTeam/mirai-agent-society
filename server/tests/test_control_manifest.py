import hashlib
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.control_manifest import CONSTITUTION_SHA256, build_control_manifest
from app.main import POLICY_METADATA
from tests.test_endpoints import request


class ControlManifestTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_endpoint_is_small_current_and_policy_aligned(self):
        status, body = await request("/api/v1/control-manifest")
        self.assertEqual(status, 200)
        self.assertEqual(body["schema_version"], "1")
        self.assertEqual(body["manifest_version"], "1")
        self.assertEqual(body["policy_version"], POLICY_METADATA.policy_version)
        self.assertEqual(body["protocol_version"], POLICY_METADATA.protocol_version)
        self.assertEqual(body["control"]["requires_reacceptance"], POLICY_METADATA.requires_reacceptance)
        self.assertEqual(body["constitution"]["sha256"], CONSTITUTION_SHA256)
        self.assertLess(len(json.dumps(body)), 2048)
        self.assertNotIn("operator", json.dumps(body).lower())
        self.assertNotIn("private", json.dumps(body).lower())
        now = datetime.now(timezone.utc)
        generated = datetime.fromisoformat(body["generated_at"].replace("Z", "+00:00"))
        expires = datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00"))
        self.assertLessEqual(generated, now)
        self.assertGreater(expires, now)

    async def test_builder_is_deterministic_and_matches_canonical_constitution(self):
        instant = datetime(2026, 9, 23, 12, 1, tzinfo=timezone.utc)
        first = build_control_manifest(POLICY_METADATA, now=instant)
        second = build_control_manifest(POLICY_METADATA, now=instant.replace(second=55))
        self.assertEqual(first, second)
        path = Path(__file__).resolve().parents[2] / "docs" / "mas_constitution_v1.json"
        self.assertEqual(CONSTITUTION_SHA256, "15173811c09fd646c16c392ab8038a2c237763c63e6391f67aa1da1de2d9ddbb")
        if path.exists():
            document = json.loads(path.read_text(encoding="utf-8"))
            canonical = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            self.assertEqual(hashlib.sha256(canonical).hexdigest(), CONSTITUTION_SHA256)


if __name__ == "__main__":
    unittest.main()
