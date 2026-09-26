"""Offline M8.2.3 package change and governance gates."""

import copy
import hashlib
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from client.mas_client.agent_package_update import check_agent_package, package_governance_matches, read_package_state
from client.mas_client.control_plane import OperatorPermissions, evaluate_control
from client.mas_client.local_state import LocalStateStore, StateValidationError
from client.mas_client.resident_readiness import apply_reviewed_governance
from client.tests.package_fixture import FakePackageTransport, ORIGIN
from client.tests.test_control_plane import manifest as control_manifest
from client.tests.test_local_state import IDENTITY, PROFILE, STATE

NOW = datetime(2026, 9, 23, 12, 1, tzinfo=UTC)
OPERATOR = OperatorPermissions(True, True, True)


class PackageUpdateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = LocalStateStore.for_agent(IDENTITY["agent_id"], Path(temporary.name) / "agents")
        self.store.provision_identity(copy.deepcopy(IDENTITY))
        self.store.write_profile(copy.deepcopy(PROFILE))
        state = copy.deepcopy(STATE)
        state["control_versions"] = {"policy": "0.1", "protocol": "0.1", "manifest": "1"}
        state["observed_versions"] = {"policy": "0.1", "protocol": "0.1", "manifest": "1"}
        self.store.write_state(state)
        self.transport = FakePackageTransport()
        self._apply_current(True)

    def _apply_current(self, required):
        item = self.transport.manifest
        policy = self.transport.resources["policy"]
        protocol = self.transport.resources["protocol"]
        apply_reviewed_governance(self.store, package=item, policy_bytes=policy,
                                  protocol_bytes=protocol, accepted_at="2026-09-23T11:00:00Z",
                                  acceptance_reference="genuine-offline-fixture-review",
                                  requires_reacceptance=required)

    def _change(self, resource_id, raw, version=None):
        self.transport.resources[resource_id] = raw
        item = next(item for item in self.transport.manifest["required_documents"] if item["id"] == resource_id)
        item["sha256"] = hashlib.sha256(raw).hexdigest()
        if version is not None:
            item["version"] = version

    def _control(self, *, policy="0.1", protocol="0.1", reaccept=False, min_protocol="0.1"):
        item = control_manifest()
        item["policy_version"] = policy
        item["protocol_version"] = protocol
        item["control"]["requires_reacceptance"] = reaccept
        item["compatibility"]["min_protocol_version"] = min_protocol
        return evaluate_control(self.store, expected_origin=ORIGIN, operator=OPERATOR,
                                now=NOW, live_status=200, live_manifest=item)

    def test_unchanged_package_is_two_lightweight_requests_and_no_redownload(self):
        changed = check_agent_package(self.store, self.transport, now=NOW)
        self.assertEqual(len(changed), 9)
        initial = read_package_state(self.store)
        self.assertEqual(len(initial["resources"]), 9)
        self.assertTrue(all(item["last_verified_at"] for item in initial["resources"].values()))
        self.assertEqual(initial["agent_id"], IDENTITY["agent_id"])
        self.transport.resource_calls.clear()
        self.transport.reload_calls.clear()
        self.transport.manifest["generated_at"] = "2026-09-23T12:01:00Z"
        self.assertEqual(check_agent_package(self.store, self.transport, now=NOW), {})
        self.assertEqual(self.transport.resource_calls, [])
        self.assertEqual(self.transport.reload_calls, [])
        self.assertEqual(initial["manifest_fingerprint"], read_package_state(self.store)["manifest_fingerprint"])
        self.assertEqual(len(self.transport.package_calls), 2)

    def test_changed_skill_downloads_only_skill_and_reloads_before_decision(self):
        check_agent_package(self.store, self.transport, now=NOW)
        self.transport.resource_calls.clear()
        self.transport.reload_calls.clear()
        self._change("skill", b"Updated MAS skill guidance")
        changed = check_agent_package(self.store, self.transport, now=NOW)
        self.assertEqual(set(changed), {"skill"})
        self.assertEqual(self.transport.resource_calls,
                         [next(item["url"] for item in self.transport.manifest["required_documents"] if item["id"] == "skill")])
        self.assertEqual(self.transport.reload_calls, [{"skill": b"Updated MAS skill guidance"}])
        self.assertEqual(read_package_state(self.store)["pending_guidance"], [])

    def test_hash_mismatch_wrong_origin_redirect_and_missing_required_fail_closed(self):
        check_agent_package(self.store, self.transport, now=NOW)
        original = copy.deepcopy(self.transport.manifest)
        self._change("skill", b"New skill")
        self.transport.resources["skill"] = b"wrong bytes"
        with self.assertRaises(StateValidationError):
            check_agent_package(self.store, self.transport, now=NOW)
        incomplete = read_package_state(self.store)
        self.assertNotEqual(incomplete["observed_manifest_fingerprint"], incomplete["manifest_fingerprint"])
        self.assertFalse(package_governance_matches(self.store))
        self.transport.manifest = copy.deepcopy(original)
        self.transport.manifest["required_documents"][0]["url"] = "https://elsewhere.example/agent-resources/skill/SKILL.md"
        with self.assertRaises(StateValidationError):
            check_agent_package(self.store, self.transport, now=NOW)
        self.transport.manifest = copy.deepcopy(original)
        self.transport.redirect_package = True
        with self.assertRaises(StateValidationError):
            check_agent_package(self.store, self.transport, now=NOW)
        self.transport.redirect_package = False
        self._change("skill", b"Actually changed")
        self.transport.redirect_resource = True
        with self.assertRaises(StateValidationError):
            check_agent_package(self.store, self.transport, now=NOW)
        self.transport.redirect_resource = False
        self.transport.manifest = copy.deepcopy(original)
        self.transport.manifest["required_documents"] = [item for item in original["required_documents"] if item["id"] != "policy"]
        with self.assertRaises(StateValidationError):
            check_agent_package(self.store, self.transport, now=NOW)
        self.assertEqual(self.store.read_identity(), IDENTITY)

    def test_malformed_manifest_fails_without_replacing_verified_state(self):
        check_agent_package(self.store, self.transport, now=NOW)
        verified = read_package_state(self.store)["manifest_fingerprint"]
        self.transport.manifest["emergency_fallback"] = {"unexpected": "object"}
        with self.assertRaises(StateValidationError):
            check_agent_package(self.store, self.transport, now=NOW)
        self.assertEqual(read_package_state(self.store)["manifest_fingerprint"], verified)
        self.assertEqual(self.store.read_identity(), IDENTITY)

    def test_policy_refresh_without_reacceptance_is_not_operator_acceptance(self):
        check_agent_package(self.store, self.transport, now=NOW)
        self._change("policy", b"Policy 0.2 reviewed by runtime", "0.2")
        self.assertEqual(set(check_agent_package(self.store, self.transport, now=NOW)), {"policy"})
        self.assertFalse(self._control(policy="0.2").may_write)
        self._apply_current(False)
        self.assertTrue(package_governance_matches(self.store))
        self.assertTrue(self._control(policy="0.2").may_write)
        acceptance = self.store.read_private_document("policy-acceptance.json")
        self.assertEqual(acceptance["policy_version"], "0.1")  # no fabricated reacceptance

    def test_policy_reacceptance_requires_new_operator_evidence(self):
        check_agent_package(self.store, self.transport, now=NOW)
        self._change("policy", b"Policy 0.2 requiring acceptance", "0.2")
        check_agent_package(self.store, self.transport, now=NOW)
        self.assertFalse(self._control(policy="0.2", reaccept=True).may_write)
        self._apply_current(False)
        decision = self._control(policy="0.2", reaccept=True)
        self.assertTrue(decision.must_reaccept)
        self.assertFalse(decision.may_write)
        self._apply_current(True)
        self.assertTrue(self._control(policy="0.2", reaccept=True).may_write)

    def test_incompatible_protocol_and_constitution_mismatch_fail_closed(self):
        check_agent_package(self.store, self.transport, now=NOW)
        self._change("protocol", b"Protocol 2.0", "2.0")
        check_agent_package(self.store, self.transport, now=NOW)
        self.assertEqual(self._control(protocol="2.0", min_protocol="2.0").stop_reason, "protocol_incompatible")
        self.transport.manifest["constitution"]["sha256"] = "0" * 64
        with self.assertRaises(StateValidationError):
            check_agent_package(self.store, self.transport, now=NOW)
        self.assertEqual(self.store.read_identity(), IDENTITY)
        self.assertTrue(read_package_state(self.store)["constitution_hold"])

    def test_human_constitution_change_holds_writes_even_with_same_machine_hash(self):
        check_agent_package(self.store, self.transport, now=NOW)
        self._change("constitution", b"Reworded human Constitution without a successor")
        check_agent_package(self.store, self.transport, now=NOW)
        self.assertTrue(read_package_state(self.store)["constitution_hold"])
        self.assertFalse(package_governance_matches(self.store))
        self.assertEqual(self.store.read_identity(), IDENTITY)

    def test_documentation_only_change_refreshes_without_reload_or_reacceptance(self):
        check_agent_package(self.store, self.transport, now=NOW)
        self.transport.resource_calls.clear()
        self.transport.reload_calls.clear()
        self._change("privacy", b"Privacy clarification")
        self.assertEqual(set(check_agent_package(self.store, self.transport, now=NOW)), {"privacy"})
        self.assertEqual(len(self.transport.resource_calls), 1)
        self.assertEqual(self.transport.reload_calls, [])
        self.assertTrue(package_governance_matches(self.store))

    def test_restart_retries_pending_guidance_and_corrupt_state_does_not_reset_identity(self):
        check_agent_package(self.store, self.transport, now=NOW)
        self._change("skill", b"Restart needs new Skill")
        def fail(_documents):
            raise OSError("runtime cannot reload")
        self.transport.reload_guidance = fail
        with self.assertRaises(OSError):
            check_agent_package(self.store, self.transport, now=NOW)
        self.assertEqual(read_package_state(self.store)["pending_guidance"], ["skill"])
        restarted = LocalStateStore.for_agent(IDENTITY["agent_id"], self.store.root.parent)
        transport = FakePackageTransport()
        transport.manifest = copy.deepcopy(self.transport.manifest)
        transport.resources = copy.deepcopy(self.transport.resources)
        check_agent_package(restarted, transport, now=NOW)
        self.assertEqual(transport.resource_calls, [])
        self.assertEqual(transport.reload_calls, [{"skill": b"Restart needs new Skill"}])
        self.assertEqual(read_package_state(restarted)["pending_guidance"], [])
        budget_before = restarted.read_state()["rolling_check_timestamps"][:]
        (restarted.root / "agent-package-state.json").write_text("{broken", encoding="utf-8")
        with self.assertRaises(StateValidationError):
            check_agent_package(restarted, transport, now=NOW)
        self.assertEqual(restarted.read_identity(), IDENTITY)
        self.assertEqual(restarted.read_state()["rolling_check_timestamps"], budget_before)
