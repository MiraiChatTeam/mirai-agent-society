"""Focused M3.9B tests: applied versions and revalidation are not actions."""

import copy
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from client.mas_client.control_plane import OperatorPermissions, evaluate_control, read_cached_manifest
from client.mas_client.local_state import LocalStateStore
from client.tests.test_control_plane import NOW, ORIGIN, manifest
from client.tests.test_local_state import IDENTITY, PROFILE, STATE


class ControlSemanticsRefinementTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = LocalStateStore(Path(temporary.name) / ".mas")
        self.store.provision_identity(copy.deepcopy(IDENTITY))
        self.store.write_profile(copy.deepcopy(PROFILE))
        state = copy.deepcopy(STATE)
        state["control_versions"].update(policy="0.1", protocol="0.1")
        self.store.write_state(state)
        self.operator = OperatorPermissions(may_read=True, may_write=True, may_create_thread=True)

    def evaluate(self, item, *, now=NOW, status=200, cached=None):
        return evaluate_control(
            self.store, expected_origin=ORIGIN, operator=self.operator, now=now,
            live_status=status, live_manifest=item if status == 200 else None,
            cached_manifest=cached,
        )

    def test_advertised_policy_and_protocol_are_not_locally_applied(self):
        item = manifest()
        item["policy_version"] = "0.2"
        item["protocol_version"] = "0.2"
        result = self.evaluate(item)
        self.assertTrue(result.must_refresh_policy)
        self.assertFalse(result.may_write)
        self.assertEqual(result.stop_reason, "policy_refresh_required")
        self.assertEqual(self.store.read_state()["control_versions"], {
            "policy": "0.1", "protocol": "0.1", "manifest": "1"
        })

        # A separately completed policy/protocol application is required.
        state = self.store.read_state()
        state["control_versions"].update(policy="0.2", protocol="0.2")
        self.store.write_state(state)
        result = self.evaluate(item)
        self.assertFalse(result.must_refresh_policy)
        self.assertTrue(result.may_write)

        item["control"]["requires_policy_refresh"] = True
        result = self.evaluate(item)
        self.assertTrue(result.must_refresh_policy)
        self.assertFalse(result.may_write)

    def test_protocol_mismatch_alone_blocks_autonomous_writes(self):
        item = manifest()
        item["protocol_version"] = "0.2"
        result = self.evaluate(item)
        self.assertFalse(result.must_refresh_policy)
        self.assertFalse(result.may_write)
        self.assertEqual(result.stop_reason, "protocol_refresh_required")
        self.assertEqual(self.store.read_state()["control_versions"]["protocol"], "0.1")

    def test_check_after_is_revalidation_guidance_not_a_wake_or_action(self):
        item = manifest()
        item["control"]["check_after_seconds"] = 120
        result = self.evaluate(item)
        self.assertEqual(result.retry_after, 120)
        self.assertTrue(result.may_read and result.may_write)
        state = self.store.read_state()
        self.assertIsNone(state["last_run_at"])
        self.assertEqual(state["rolling_action_timestamps"], [])

        # A still-fresh cached result can be reused at an earlier wake.
        cached = read_cached_manifest(self.store)
        result = self.evaluate(None, now=NOW + timedelta(seconds=30), status=503, cached=cached)
        self.assertEqual(result.source, "cache")
        self.assertTrue(result.may_write)

        # The recheck interval never extends the manifest's actual expiry.
        item["control"]["check_after_seconds"] = 3600
        self.evaluate(item)
        cached = read_cached_manifest(self.store)
        result = self.evaluate(None, now=NOW + timedelta(minutes=5), status=503, cached=cached)
        self.assertFalse(result.may_write)
        self.assertEqual(result.stop_reason, "no_trustworthy_manifest")


if __name__ == "__main__":
    unittest.main()
