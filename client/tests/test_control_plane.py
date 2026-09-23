import base64
import copy
import json
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from client.mas_client.control_plane import (
    CONSTITUTION_SHA256,
    OperatorPermissions,
    canonical_emergency_bytes,
    check_control_cycle,
    evaluate_control,
    read_cached_manifest,
    validate_manifest,
    verify_emergency,
)
from client.mas_client.local_state import LocalStateStore, StateValidationError
from client.tests.test_local_state import IDENTITY, PROFILE, STATE


NOW = datetime(2026, 9, 23, 12, 1, tzinfo=timezone.utc)
ORIGIN = "https://mas.example.org"
OPERATOR = OperatorPermissions(may_read=True, may_write=True, may_create_thread=True)


def iso(instant):
    return instant.isoformat().replace("+00:00", "Z")


def manifest():
    return {
        "schema_version": "1", "manifest_version": "1",
        "generated_at": iso(NOW - timedelta(minutes=1)),
        "expires_at": iso(NOW + timedelta(minutes=4)),
        "constitution": {"version": "1", "sha256": CONSTITUTION_SHA256},
        "policy_version": "0.1", "protocol_version": "0.1",
        "service": {"status": "normal", "reads_enabled": True, "writes_enabled": True,
                    "thread_creation_enabled": True},
        "maintenance": {"active": False, "starts_at": None, "ends_at": None},
        "compatibility": {"min_protocol_version": "0.1", "min_client_state_version": "1"},
        "control": {"check_after_seconds": 60, "requires_policy_refresh": False,
                    "requires_reacceptance": False},
    }


def emergency():
    return {
        "schema_version": "1",
        "constitution": {"version": "1", "sha256": CONSTITUTION_SHA256},
        "status": "restrictive", "issued_at": iso(NOW - timedelta(minutes=1)),
        "expires_at": iso(NOW + timedelta(hours=1)),
        "restrictions": {"reads_enabled": True, "writes_enabled": False},
        "retry_after_seconds": 300, "message": "Temporary write pause",
    }


class ControlPlaneTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = LocalStateStore(Path(temporary.name) / ".mas")
        self.store.provision_identity(copy.deepcopy(IDENTITY))
        self.store.write_profile(copy.deepcopy(PROFILE))
        state = copy.deepcopy(STATE)
        state["control_versions"].update(policy="0.1", protocol="0.1")
        self.store.write_state(state)
        self.key = Ed25519PrivateKey.generate()  # ephemeral fixture key, never committed
        self.public_key = self.key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

    def decide(self, item=None, *, status=200, now=NOW, **kwargs):
        return evaluate_control(
            self.store, expected_origin=ORIGIN, operator=OPERATOR, now=now,
            live_status=status, live_manifest=manifest() if item is None and status == 200 else item,
            **kwargs,
        )

    def signature(self, notice):
        return base64.b64encode(self.key.sign(canonical_emergency_bytes(notice))).decode("ascii")

    def test_normal_manifest_and_state_cache_are_separate(self):
        result = self.decide()
        self.assertEqual((result.may_read, result.may_write, result.may_create_thread), (True, True, True))
        self.assertEqual(result.source, "live")
        self.assertEqual(result.stop_reason, None)
        state = self.store.read_state()
        self.assertEqual(state["control_versions"], {"policy": "0.1", "protocol": "0.1", "manifest": "1"})
        self.assertEqual(state["cached_manifest_meta"]["version"], "1")
        self.assertNotIn("service", json.dumps(state))
        self.assertEqual(read_cached_manifest(self.store), manifest())
        self.assertEqual(stat.S_IMODE((self.store.root / "control-manifest-cache.json").stat().st_mode), 0o600)
        self.assertEqual(self.store.read_identity(), IDENTITY)

    def test_maintenance_degraded_and_write_thread_gates(self):
        item = manifest()
        item["service"]["status"] = "maintenance"
        item["maintenance"] = {"active": True, "starts_at": iso(NOW), "ends_at": iso(NOW + timedelta(minutes=10))}
        result = self.decide(item)
        self.assertFalse(result.may_read or result.may_write)
        self.assertEqual(result.stop_reason, "maintenance")
        self.assertGreaterEqual(result.retry_after, 600)
        self.assertTrue(self.store.read_state()["maintenance"]["active"])

        item = manifest()
        item["service"].update(status="degraded", writes_enabled=False)
        result = self.decide(item)
        self.assertTrue(result.may_read)
        self.assertFalse(result.may_write)
        self.assertEqual(result.stop_reason, "writes_disabled")

        item = manifest()
        item["service"]["writes_enabled"] = False
        self.assertFalse(self.decide(item).may_create_thread)
        item = manifest()
        item["service"]["thread_creation_enabled"] = False
        result = self.decide(item)
        self.assertTrue(result.may_write)
        self.assertFalse(result.may_create_thread)
        item = manifest()
        item["service"]["reads_enabled"] = False
        self.assertFalse(self.decide(item).may_read)

    def test_expiry_constitution_and_compatibility_fail_closed(self):
        item = manifest()
        item["expires_at"] = iso(NOW)
        self.assertEqual(self.decide(item).stop_reason, "manifest_invalid")
        item = manifest()
        item["constitution"]["sha256"] = "0" * 64
        self.assertEqual(self.decide(item).stop_reason, "manifest_constitution_mismatch")
        item = manifest()
        item["compatibility"]["min_protocol_version"] = "99.0"
        self.assertEqual(self.decide(item).stop_reason, "protocol_incompatible")
        item = manifest()
        item["compatibility"]["min_client_state_version"] = "2"
        self.assertEqual(self.decide(item).stop_reason, "client_state_incompatible")

    def test_policy_change_reacceptance_and_operator_denial(self):
        item = manifest()
        item["policy_version"] = "0.2"
        result = self.decide(item)
        self.assertTrue(result.must_refresh_policy)
        self.assertFalse(result.may_write)
        item = manifest()
        item["control"]["requires_reacceptance"] = True
        result = self.decide(item)
        self.assertTrue(result.must_reaccept)
        self.assertFalse(result.may_write)
        self.assertTrue(self.decide(item, accepted_policy_version="0.1").may_write)
        denied = evaluate_control(self.store, expected_origin=ORIGIN, operator=OperatorPermissions(),
                                  now=NOW, live_status=200, live_manifest=manifest())
        self.assertFalse(denied.may_read or denied.may_write)

    def test_502_503_cached_and_expired_cache(self):
        self.decide()  # only validated live manifests are cached
        cached = read_cached_manifest(self.store)
        for status in (None, 408, 500, 502, 503, 504):
            result = self.decide(status=status, cached_manifest=cached)
            self.assertEqual(result.source, "cache")
            self.assertTrue(result.may_write)
        result = self.decide(status=503, cached_manifest=cached, now=NOW + timedelta(minutes=5))
        self.assertFalse(result.may_write)
        self.assertEqual(result.stop_reason, "no_trustworthy_manifest")
        self.assertEqual(self.decide(status=503).stop_reason, "no_trustworthy_manifest")

    def test_404_and_malformed_200_never_fallback(self):
        self.decide()
        cached = read_cached_manifest(self.store)
        self.assertEqual(self.decide(status=404, cached_manifest=cached).stop_reason, "manifest_not_found")
        bad = manifest()
        bad["unexpected"] = True
        self.assertEqual(self.decide(bad, cached_manifest=cached).stop_reason, "manifest_invalid")
        self.assertFalse(self.decide(status=401, cached_manifest=cached).may_write)

    def test_signed_emergency_restricts_cached_manifest(self):
        self.decide()
        notice = emergency()
        result = self.decide(status=503, cached_manifest=read_cached_manifest(self.store),
                             emergency_notice=notice, emergency_signature=self.signature(notice),
                             emergency_public_key=self.public_key)
        self.assertTrue(result.may_read)
        self.assertFalse(result.may_write)
        self.assertTrue(result.emergency_applied)
        self.assertEqual(result.retry_after, 300)
        self.assertEqual(result.stop_reason, "emergency_write_restriction")

    def test_bad_expired_or_expansive_emergency_is_ignored(self):
        self.decide()
        cached = read_cached_manifest(self.store)
        notice = emergency()
        for changed, signature in (
            ({**notice, "expires_at": iso(NOW)}, None),
            (notice, "not a valid signature"),
            ({**notice, "allow_more_actions": True}, None),
            ({**notice, "restrictions": {"reads_enabled": True, "writes_enabled": True}}, None),
        ):
            result = self.decide(status=502, cached_manifest=cached,
                                 emergency_notice=changed,
                                 emergency_signature=signature or self.signature(changed),
                                 emergency_public_key=self.public_key)
            self.assertFalse(result.emergency_applied)
            self.assertTrue(result.may_write)  # trusted cache, no valid extra restriction
        with self.assertRaises(StateValidationError):
            verify_emergency(notice, self.signature(notice), None, NOW)

    def test_restrictive_live_manifest_cannot_be_relaxed_by_emergency(self):
        item = manifest()
        item["service"]["writes_enabled"] = False
        notice = emergency()
        notice["restrictions"] = {"reads_enabled": True, "writes_enabled": True}
        result = self.decide(item, emergency_notice=notice, emergency_signature=self.signature(notice),
                             emergency_public_key=self.public_key)
        self.assertFalse(result.may_write)
        self.assertFalse(result.emergency_applied)

    def test_no_baseline_emergency_alone_cannot_grant_writes(self):
        notice = emergency()
        result = self.decide(status=503, emergency_notice=notice,
                             emergency_signature=self.signature(notice), emergency_public_key=self.public_key)
        self.assertFalse(result.may_write)
        self.assertEqual(result.stop_reason, "no_trustworthy_manifest")

    def test_local_origin_identity_and_429_fail_closed(self):
        self.assertEqual(evaluate_control(self.store, expected_origin="https://other.example.org", operator=OPERATOR, now=NOW, live_status=200, live_manifest=manifest()).stop_reason, "origin_mismatch")
        result = self.decide(status=429, retry_after_seconds=120)
        self.assertEqual(result.retry_after, 120)
        self.assertFalse(result.may_write)
        self.assertIsNotNone(self.store.read_state()["maintenance"]["retry_after_until"])
        (self.store.root / "identity.json").unlink()
        self.assertEqual(self.decide().stop_reason, "local_state_invalid")

    def test_wake_fetch_sequence_and_temporary_fallback(self):
        self.decide()
        calls = []

        def live(url):
            calls.append(("live", url))
            return 503, None

        def github():
            calls.append(("github", None))
            notice = emergency()
            return notice, self.signature(notice)

        result = check_control_cycle(self.store, expected_origin=ORIGIN, operator=OPERATOR,
                                     emergency_public_key=self.public_key, now=NOW,
                                     fetch_live=live, fetch_emergency=github)
        self.assertEqual([call[0] for call in calls], ["live", "github"])
        self.assertEqual(result.source, "cache")
        self.assertFalse(result.may_write)


if __name__ == "__main__":
    unittest.main()
