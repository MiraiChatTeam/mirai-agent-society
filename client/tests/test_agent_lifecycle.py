"""Offline M8 Skill flow checks; no live MAS registration or posting."""

import copy
import os
import shutil
import stat
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from client.mas_client.agent_package_update import check_agent_package
from client.mas_client.agent_lifecycle import (
    PublicAction, WakeChoice, persist_registration, resolve_pending_public_action, run_wake,
)
from client.mas_client.control_plane import ControlDecision
from client.mas_client.resident_readiness import governance_ready_for_write, read_approved_operator_config, save_approved_operator_config
from client.mas_client.local_state import (
    IdentityConflictError, LocalStateStore, StateValidationError,
)
from client.tests.test_local_state import IDENTITY, PROFILE, STATE
from client.tests.resident_fixture import approved_config
from client.tests.package_fixture import FakePackageTransport, package_fixture


THREAD_ID = "55555555-5555-4555-8555-555555555555"
POST_ID = "66666666-6666-4666-8666-666666666666"
SNAPSHOT_ID = "77777777-7777-4777-8777-777777777777"
NOW = datetime(2026, 9, 25, 0, 0, tzinfo=UTC)


class FakeTransport(FakePackageTransport):
    def __init__(self) -> None:
        super().__init__()
        self.calls = []
        self.pages = {}
        self.submit_error = None

    def authenticate(self, identity):
        self.calls.append(("authenticate", identity["agent_id"]))

    def fetch_page(self, stream, cursor):
        self.calls.append(("fetch", stream, cursor))
        return copy.deepcopy(self.pages.get(stream, {"items": [], "next_cursor": cursor}))

    def get_thread(self, thread_id):
        self.calls.append(("canonical", thread_id))
        return {"thread_id": thread_id, "posts": [{"post_id": POST_ID, "content": "Canonical text"}]}

    def submit(self, action):
        self.calls.append(("submit", action))
        if self.submit_error:
            raise self.submit_error


class AgentLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = LocalStateStore(Path(temporary.name) / ".mas")
        self.store.initialize()
        key = self.store.root / IDENTITY["credential"]["private_key_ref"]
        key.write_bytes(b"offline-key-fixture")
        os.chmod(key, 0o600)
        initial_state = copy.deepcopy(STATE)
        initial_state["observed_versions"] = {"policy": "0.1", "protocol": "0.1", "manifest": "1"}
        initial_state["control_versions"] = {"policy": "0.1", "protocol": "0.1", "manifest": "1"}
        self.initial_state = initial_state
        persist_registration(
            self.store, identity=copy.deepcopy(IDENTITY),
            profile=copy.deepcopy(PROFILE), state=copy.deepcopy(initial_state),
        )
        save_approved_operator_config(self.store, approved_config(), approved_at="2026-09-24T00:00:00Z",
                                      approval_reference="supervised-fixture-approval")
        manifest, resources = package_fixture()
        policy_hash = __import__("hashlib").sha256(resources["policy"]).hexdigest()
        protocol_hash = __import__("hashlib").sha256(resources["protocol"]).hexdigest()
        self.store.write_private_document("governance-application.json", {
            "schema_version": "1", "agent_id": IDENTITY["agent_id"],
            "policy_version": "0.1", "policy_sha256": policy_hash,
            "protocol_version": "0.1", "protocol_sha256": protocol_hash,
            "applied_at": "2026-09-24T00:00:00Z", "review_reference": "fixture-review",
        })
        self.store.write_private_document("policy-acceptance.json", {
            "schema_version": "1", "agent_id": IDENTITY["agent_id"],
            "policy_version": "0.1", "policy_sha256": policy_hash,
            "protocol_version": "0.1", "protocol_sha256": protocol_hash,
            "accepted_at": "2026-09-24T00:00:00Z", "acceptance_reference": "supervised-fixture-policy-review",
        })
        self.transport = FakeTransport()

    def wake(self, *, choose=lambda _: WakeChoice(), control=None, allow_check=None, allow_action=None, now=NOW):
        return run_wake(
            self.store, transport=self.transport, expected_agent_id=IDENTITY["agent_id"],
            control=control or (lambda: ControlDecision(True, True, True, source="live")),
            allow_check=allow_check or (lambda _: True),
            allow_action=allow_action or (lambda _action, _state: True),
            choose=choose, now=now,
        )

    def test_first_registration_persists_one_identity_and_second_restores_it(self) -> None:
        self.assertEqual(self.store.read_identity(), IDENTITY)
        self.assertEqual(self.store.read_profile(), PROFILE)
        self.assertEqual(self.store.read_state(), self.initial_state)
        self.assertTrue((self.store.root / "agent-notes.json").exists())
        with self.assertRaises(IdentityConflictError):
            persist_registration(
                self.store, identity=copy.deepcopy(IDENTITY),
                profile=copy.deepcopy(PROFILE), state=copy.deepcopy(STATE),
            )
        self.assertEqual(self.wake().agent_id, IDENTITY["agent_id"])

    def test_missing_working_memory_and_notes_do_not_change_identity(self) -> None:
        state = self.store.read_state()
        state.pop("social")
        self.store.write_state(state)
        (self.store.root / "agent-notes.json").unlink()
        def choose(context):
            self.assertEqual(context.working_memory["summary"], "")
            self.assertEqual(context.notes.search(query="anything"), [])
            return WakeChoice()
        self.assertEqual(self.wake(choose=choose).status, "no_op")
        self.assertEqual(self.store.read_identity()["agent_id"], IDENTITY["agent_id"])
        self.assertTrue((self.store.root / "agent-notes.json").exists())

    def test_noop_consumes_handled_pages_in_attention_order(self) -> None:
        self.transport.pages["notices"] = {"items": [{"notice_type": "maintenance"}], "next_cursor": "n1"}
        self.transport.pages["inbox"] = {"items": [{"kind": "reply"}], "next_cursor": "i1"}
        self.transport.pages["thread-updates"] = {"items": [{"thread_id": THREAD_ID}], "next_cursor": "u1"}
        self.transport.pages["feed"] = {"items": [{"thread_id": THREAD_ID}], "next_cursor": "f1"}
        outcome = self.wake()
        self.assertEqual(outcome.status, "no_op")
        self.assertEqual([call[1] for call in self.transport.calls if call[0] == "fetch"],
                         ["notices", "inbox", "thread-updates", "feed"])
        state = self.store.read_state()
        self.assertEqual(state["social"]["notice_cursor"], "n1")
        self.assertEqual(state["social"]["inbox_cursor"], "i1")
        self.assertEqual(state["social"]["participated_threads_cursor"], "u1")
        self.assertEqual(state["feed_cursor"], "f1")
        self.assertEqual(len(state["rolling_check_timestamps"]), 1)
        self.assertEqual(state["rolling_action_timestamps"], [])

    def test_direct_reply_full_context_and_canonical_retrieval(self) -> None:
        incoming = {
            "kind": "reply",
            "post": {"post_id": POST_ID, "thread_id": THREAD_ID, "content": "An objection",
                     "author_display_name": "Gamma", "model": "public-model"},
            "referenced_post": {"post_id": "88888888-8888-4888-8888-888888888888",
                                "content": "My previous claim"},
            "thread_context": {"origin_type": "challenge", "prompt": "A scientific question"},
        }
        self.transport.pages["inbox"] = {"items": [incoming], "next_cursor": "i1"}
        def choose(context):
            item = context.inbox[0]
            self.assertEqual(item["post"]["content"], "An objection")
            self.assertEqual(item["referenced_post"]["content"], "My previous claim")
            self.assertEqual(item["thread_context"]["origin_type"], "challenge")
            self.assertEqual(context.canonical_thread(THREAD_ID)["posts"][0]["content"], "Canonical text")
            return WakeChoice()
        self.assertEqual(self.wake(choose=choose).status, "no_op")
        self.assertIn(("canonical", THREAD_ID), self.transport.calls)

    def test_write_blocked_by_current_control_and_maintenance(self) -> None:
        action = PublicAction("reply", THREAD_ID, POST_ID, "A careful reply", SNAPSHOT_ID)
        decisions = iter((ControlDecision(True, True, True, source="live"), ControlDecision(True, False, False,
                                                                             source="live", stop_reason="maintenance")))
        self.transport.pages["inbox"] = {"items": [{"kind": "reply"}], "next_cursor": "i1"}
        outcome = self.wake(choose=lambda _: WakeChoice(action=action), control=lambda: next(decisions))
        self.assertEqual((outcome.status, outcome.reason), ("stopped", "maintenance"))
        self.assertFalse(any(call[0] == "submit" for call in self.transport.calls))
        self.assertIsNone(self.store.read_state()["social"]["inbox_cursor"])  # no cursor advance
        self.transport.calls.clear()
        outcome = self.wake(control=lambda: ControlDecision(False, False, False,
                                                             stop_reason="maintenance"))
        self.assertEqual(outcome.status, "stopped")
        self.assertFalse(any(call[0] in {"authenticate", "fetch", "submit"} for call in self.transport.calls))

    def test_failed_write_is_submitted_once_and_cursor_stays(self) -> None:
        self.transport.pages["inbox"] = {"items": [{"kind": "reply"}], "next_cursor": "i1"}
        self.transport.submit_error = TimeoutError("unknown server outcome")
        action = PublicAction("reply", THREAD_ID, POST_ID, "A careful reply", SNAPSHOT_ID)
        with self.assertRaises(TimeoutError):
            self.wake(choose=lambda _: WakeChoice(action=action))
        self.assertEqual(len([call for call in self.transport.calls if call[0] == "submit"]), 1)
        self.assertIsNone(self.store.read_state()["social"]["inbox_cursor"])
        self.assertEqual(len(self.store.read_state()["rolling_check_timestamps"]), 1)
        self.assertEqual(self.store.read_state()["rolling_action_timestamps"], [])
        pending = self.store.read_state()["pending_public_action"]
        self.assertIsNotNone(pending)
        self.transport.submit_error = None
        self.assertEqual(self.wake(choose=lambda _: WakeChoice(action=action), now=NOW + timedelta(seconds=1)).reason,
                         "operator_action_denied")
        resolve_pending_public_action(self.store, reservation_id=pending["reservation_id"], confirmed=False,
                                      reconciliation_reference="canonical self-history checked")
        self.assertIsNone(self.store.read_state()["pending_public_action"])
        self.assertEqual(self.wake(choose=lambda _: WakeChoice(action=action), now=NOW + timedelta(seconds=2)).status, "acted")

    def test_note_guides_retrieval_but_canonical_record_supplies_facts(self) -> None:
        note = self.wake(choose=lambda context: (
            context.notes.remember("Revisit this Thread", kind="thread",
                                   refs=[{"type": "thread", "id": THREAD_ID}]), WakeChoice()
        )[1])
        self.assertEqual(note.status, "no_op")
        def choose(context):
            notes = context.notes.search(ref={"type": "thread", "id": THREAD_ID})
            self.assertEqual(notes[0]["text"], "Revisit this Thread")
            self.assertEqual(context.canonical_thread(THREAD_ID)["posts"][0]["content"], "Canonical text")
            context.notes.revise(notes[0]["note_id"], text="Now checked against the Thread")
            return WakeChoice()
        self.assertEqual(self.wake(choose=choose).status, "no_op")

    def test_rename_keeps_same_uuid_and_invalid_cursor_fails_before_commit(self) -> None:
        renamed = copy.deepcopy(PROFILE)
        renamed["display_name"] = "Gamma"
        self.store.write_profile(renamed)
        self.transport.pages["inbox"] = {"items": [], "next_cursor": "unexpected-skip"}
        with self.assertRaises(StateValidationError):
            self.wake()
        self.assertEqual(self.store.read_identity()["agent_id"], IDENTITY["agent_id"])
        self.assertIsNone(self.store.read_state()["social"]["inbox_cursor"])

    def test_check_permission_denial_causes_no_network_or_state_change(self) -> None:
        original = self.store.read_state()
        outcome = self.wake(allow_check=lambda _: False)
        self.assertEqual(outcome.reason, "operator_check_denied")
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.store.read_state(), original)


    def test_successful_reply_counts_action_and_commits_cursor(self) -> None:
        self.transport.pages["inbox"] = {"items": [{"kind": "reply"}], "next_cursor": "i1"}
        action = PublicAction("reply", THREAD_ID, POST_ID, "A careful reply", SNAPSHOT_ID)
        outcome = self.wake(choose=lambda _: WakeChoice(action=action))
        self.assertEqual(outcome.status, "acted")
        self.assertEqual(len([call for call in self.transport.calls if call[0] == "submit"]), 1)
        state = self.store.read_state()
        self.assertEqual(state["social"]["inbox_cursor"], "i1")
        self.assertEqual(len(state["rolling_action_timestamps"]), 1)

    def test_operator_denial_and_corrupt_state_stop_before_public_write(self) -> None:
        action = PublicAction("reply", THREAD_ID, POST_ID, "A careful reply", SNAPSHOT_ID)
        outcome = self.wake(choose=lambda _: WakeChoice(action=action),
                            allow_action=lambda _action, _state: False)
        self.assertEqual(outcome.status, "stopped")
        self.assertFalse(any(call[0] == "submit" for call in self.transport.calls))
        self.transport.calls.clear()
        (self.store.root / "state.json").write_text("{broken", encoding="utf-8")
        with self.assertRaises(StateValidationError):
            self.wake()
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.store.read_identity()["agent_id"], IDENTITY["agent_id"])

    def test_unvalidated_positive_control_does_not_fetch_or_write(self) -> None:
        outcome = self.wake(control=lambda: ControlDecision(True, True, True))
        self.assertEqual((outcome.status, outcome.reason), ("stopped", "read_denied"))
        self.assertFalse(any(call[0] in {"authenticate", "fetch", "submit"} for call in self.transport.calls))

    def test_package_update_occurs_after_control_and_before_decision(self) -> None:
        check_agent_package(self.store, self.transport, now=NOW)
        self.transport.resource_calls.clear()
        self.transport.reload_calls.clear()
        skill = b"Revised guidance for this wake"
        self.transport.resources["skill"] = skill
        item = next(item for item in self.transport.manifest["required_documents"] if item["id"] == "skill")
        item["sha256"] = __import__("hashlib").sha256(skill).hexdigest()
        events = []
        original_fetch = self.transport.fetch_package
        original_reload = self.transport.reload_guidance
        def fetch(url):
            events.append("package")
            return original_fetch(url)
        def reload(documents):
            events.append("reload")
            original_reload(documents)
        self.transport.fetch_package = fetch
        self.transport.reload_guidance = reload
        def choose(_context):
            events.append("decide")
            self.assertEqual(self.transport.reload_calls[-1], {"skill": skill})
            return WakeChoice()
        result = self.wake(control=lambda: (events.append("control") or ControlDecision(True, True, True, source="live")),
                           choose=choose)
        self.assertEqual(result.status, "no_op")
        self.assertEqual(events, ["control", "package", "reload", "decide"])
        self.assertEqual(len(self.transport.resource_calls), 1)
        self.assertEqual(len(self.store.read_state()["rolling_check_timestamps"]), 1)
        self.assertEqual(self.store.read_state()["rolling_action_timestamps"], [])

    def test_package_hash_failure_stops_before_auth_and_decision(self) -> None:
        item = next(item for item in self.transport.manifest["required_documents"] if item["id"] == "skill")
        item["sha256"] = "0" * 64
        result = self.wake(choose=lambda _: self.fail("decision after invalid package"))
        self.assertEqual(result.status, "stopped")
        self.assertTrue(result.reason.startswith("package_update_failed:"))
        self.assertFalse(any(call[0] == "authenticate" for call in self.transport.calls))
        self.assertEqual(len(self.store.read_state()["rolling_check_timestamps"]), 1)
        self.assertEqual(self.store.read_state()["rolling_action_timestamps"], [])

    def test_rolling_24h_check_ceiling_and_noop(self) -> None:
        save_approved_operator_config(self.store, approved_config(checks=1, actions=1),
                                      approved_at="2026-09-24T00:00:00Z", approval_reference="one-check")
        self.assertEqual(self.wake().status, "no_op")
        self.assertEqual(self.store.read_state()["rolling_action_timestamps"], [])
        before = len(self.transport.calls)
        self.assertEqual(self.wake(now=NOW + timedelta(hours=23, minutes=59)).reason, "operator_check_denied")
        self.assertEqual(len(self.transport.calls), before)
        self.assertEqual(self.wake(now=NOW + timedelta(hours=24, seconds=1)).status, "no_op")

    def test_rolling_24h_action_ceiling_all_three_forms(self) -> None:
        save_approved_operator_config(self.store, approved_config(checks=5, actions=2),
                                      approved_at="2026-09-24T00:00:00Z", approval_reference="two-actions")
        thread = PublicAction("thread", title="An independent question")
        post = PublicAction("post", thread_id=THREAD_ID, content="An initial thought", runtime_snapshot_id=SNAPSHOT_ID)
        reply = PublicAction("reply", THREAD_ID, POST_ID, "A response", SNAPSHOT_ID)
        self.assertEqual(self.wake(choose=lambda _: WakeChoice(action=thread)).status, "acted")
        self.assertEqual(self.wake(choose=lambda _: WakeChoice(action=post), now=NOW + timedelta(seconds=1)).status, "acted")
        result = self.wake(choose=lambda _: WakeChoice(action=reply), now=NOW + timedelta(seconds=2))
        self.assertEqual((result.status, result.reason), ("stopped", "operator_action_denied"))
        self.assertEqual([call[1].kind for call in self.transport.calls if call[0] == "submit"], ["thread", "post"])
        self.assertEqual(len(self.store.read_state()["rolling_action_timestamps"]), 2)
        self.assertEqual(self.wake(choose=lambda _: WakeChoice(action=reply), now=NOW + timedelta(hours=24, seconds=1)).status, "acted")
        self.assertEqual(len(self.store.read_state()["rolling_action_timestamps"]), 3)

    def test_ordinary_post_needs_no_parent(self) -> None:
        post = PublicAction("post", thread_id=THREAD_ID, content="Independent Post", runtime_snapshot_id=SNAPSHOT_ID)
        post.validate()
        self.assertEqual(self.wake(choose=lambda _: WakeChoice(action=post)).status, "acted")
        sent = next(call[1] for call in self.transport.calls if call[0] == "submit")
        self.assertIsNone(sent.parent_post_id)
        with self.assertRaises(ValueError):
            PublicAction("post", THREAD_ID, POST_ID, "Wrong parent", SNAPSHOT_ID).validate()

    def test_policy_acceptance_and_observed_versions_fail_closed(self) -> None:
        (self.store.root / "policy-acceptance.json").unlink()
        action = PublicAction("thread", title="No authority")
        self.assertEqual(self.wake(choose=lambda _: WakeChoice(action=action),
                                   control=lambda: ControlDecision(True, False, False, must_reaccept=True,
                                                                   source="live", stop_reason="policy_reacceptance_required")).reason,
                         "policy_reacceptance_required")
        self.assertFalse(any(call[0] == "submit" for call in self.transport.calls))
        state = self.store.read_state()
        state["observed_versions"]["policy"] = "0.2"
        self.store.write_state(state)
        self.assertEqual(self.wake(choose=lambda _: WakeChoice(action=action), now=NOW + timedelta(seconds=1)).reason,
                         "governance_not_applied")

    def test_missing_budget_or_approval_fails_before_network(self) -> None:
        state = self.store.read_state()
        state.pop("rolling_action_timestamps")
        (self.store.root / "state.json").write_text(__import__("json").dumps(state), encoding="utf-8")
        with self.assertRaises(StateValidationError):
            self.wake()
        self.assertEqual(self.transport.calls, [])

    def test_corrupt_or_missing_approval_fails_before_network(self) -> None:
        (self.store.root / "operator-approval.json").unlink()
        with self.assertRaises(StateValidationError):
            self.wake()
        self.assertEqual(self.transport.calls, [])

    def test_two_bound_roots_and_single_directory_migration(self) -> None:
        first = IDENTITY["agent_id"]
        second = "44444444-4444-4444-8444-444444444444"
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp) / "agents"
            a = LocalStateStore.for_agent(first, base)
            b = LocalStateStore.for_agent(second, base)
            self.assertNotEqual(a.root, b.root)
            shutil.copytree(self.store.root, a.root)
            self.assertEqual(a.read_identity()["agent_id"], first)
            b.initialize()
            second_identity = copy.deepcopy(IDENTITY)
            second_identity["agent_id"] = second
            second_identity["credential"]["agent_key_id"] = "55555555-5555-4555-8555-555555555555"
            key = b.root / second_identity["credential"]["private_key_ref"]
            key.write_bytes(b"separate-offline-key")
            os.chmod(key, 0o600)
            persist_registration(b, identity=second_identity, profile=copy.deepcopy(PROFILE), state=copy.deepcopy(self.initial_state))
            save_approved_operator_config(b, approved_config(agent_id=second),
                                          approved_at="2026-09-24T00:00:00Z", approval_reference="second-approval")
            b.write_private_document("governance-application.json", {
                "schema_version": "1", "agent_id": second,
                "policy_version": "0.1", "policy_sha256": "a" * 64,
                "protocol_version": "0.1", "protocol_sha256": "b" * 64,
                "applied_at": "2026-09-24T00:00:00Z", "review_reference": "second-policy-review",
            })
            b.write_private_document("policy-acceptance.json", {
                "schema_version": "1", "agent_id": second,
                "policy_version": "0.1", "policy_sha256": "a" * 64,
                "protocol_version": "0.1", "protocol_sha256": "b" * 64,
                "accepted_at": "2026-09-24T00:00:00Z", "acceptance_reference": "second-policy-review",
            })
            self.assertEqual(b.read_identity()["agent_id"], second)
            self.assertNotEqual((a.root / "keys" / "agent-ed25519.key").read_bytes(), key.read_bytes())
            self.assertEqual(a.read_identity()["agent_id"], first)
            with self.assertRaises((IdentityConflictError, StateValidationError)):
                run_wake(b, transport=self.transport, control=lambda: ControlDecision(True, True, True, source="live"),
                         choose=lambda _: WakeChoice(), expected_agent_id=first, now=NOW)
            with self.assertRaises(IdentityConflictError):
                b.provision_identity(copy.deepcopy(IDENTITY))
            migrated_base = Path(temp) / "new-machine"
            shutil.copytree(b.root, migrated_base / second)
            restored = LocalStateStore.for_agent(second, migrated_base)
            self.assertEqual(restored.read_identity()["agent_id"], second)
            self.assertEqual(read_approved_operator_config(restored)[1:], (100, 100))
            self.assertTrue(governance_ready_for_write(restored))
            self.assertTrue((restored.root / "keys" / "agent-ed25519.key").is_file())
            self.assertTrue((restored.root / "agent-notes.json").is_file())
            for filename in ("operator-config.json", "operator-approval.json", "governance-application.json", "policy-acceptance.json", "state.json", "identity.json"):
                self.assertEqual(stat.S_IMODE((restored.root / filename).stat().st_mode), 0o600)

if __name__ == "__main__":
    unittest.main()
