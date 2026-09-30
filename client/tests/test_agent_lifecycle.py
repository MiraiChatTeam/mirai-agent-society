"""Offline M8 Skill flow checks; no live MAS registration or posting."""

import copy
import os
import shutil
import stat
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from client.mas_client.agent_package_update import check_agent_package
from client.mas_client.agent_lifecycle import (
    PublicAction, WakeChoice, attention_page_path, persist_confirmed_rename, persist_registration, resolve_pending_public_action, run_wake,
)
from client.mas_client.control_plane import ControlDecision
from client.mas_client.resident_readiness import governance_ready_for_write, read_approved_operator_config, save_approved_operator_config
from client.mas_client.run_journal import read_recent_runs, write_run_summary
from client.mas_client.research_telemetry import pending_events
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
        self.registration_receipt = persist_registration(
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

    def wake(self, *, choose=lambda _: WakeChoice(), control=None, allow_check=None, allow_action=None,
             approve_public_action=None, now=NOW, invocation_mode="unknown",
             attention=("inbox", "thread-updates", "feed")):
        return run_wake(
            self.store, transport=self.transport, expected_agent_id=IDENTITY["agent_id"],
            control=control or (lambda: ControlDecision(True, True, True, source="live")),
            allow_check=allow_check or (lambda _: True),
            allow_action=allow_action or (lambda _action, _state: True),
            approve_public_action=approve_public_action,
            choose=choose, choose_attention=lambda _context: attention,
            now=now, invocation_mode=invocation_mode,
        )

    def test_telemetry_is_best_effort_and_never_changes_no_op_or_public_action(self) -> None:
        self.transport.pages["feed"] = {"items": [{"thread_id": THREAD_ID, "title": "not telemetry"}], "next_cursor": None,
                                        "requested_limit": 5}
        def choose(context):
            context.mark_handled("feed")
            return WakeChoice()
        first = self.wake(choose=choose, attention=("feed",))
        self.assertEqual(first.status, "no_op")
        events = pending_events(self.store)
        kinds = [item["payload"]["event_type"] for item in events]
        self.assertEqual(kinds, ["source_fetched", "source_fetched", "source_handled", "run_outcome"])
        self.assertEqual({item["run_id"] for item in events}, {read_recent_runs(self.store)[0]["run_id"]})
        self.assertEqual(events[1]["payload"]["returned_thread_ids"], [THREAD_ID])
        self.assertTrue(events[-1]["payload"]["exposure_complete"])
        self.assertFalse(events[-1]["payload"]["stopped_early"])
        self.assertNotIn("title", str(events))
        class FailingTransport(FakeTransport):
            def submit_telemetry_batch(self, _body):
                raise TimeoutError("telemetry offline")
        failing = FailingTransport()
        self.transport = failing
        self.assertEqual(self.wake(attention=(), now=NOW + timedelta(hours=1)).status, "no_op")
        self.assertTrue(pending_events(self.store))

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

    def test_first_registration_receipt_is_private_and_nonsecret(self) -> None:
        receipt = self.registration_receipt
        self.assertEqual(receipt.kind, "registration")
        self.assertEqual(receipt.agent_id, IDENTITY["agent_id"])
        self.assertEqual(receipt.display_name, PROFILE["display_name"])
        self.assertEqual(receipt.society_origin, IDENTITY["society_origin"])
        self.assertEqual(receipt.state_root, str(self.store.root))
        message = receipt.message()
        for expected in (PROFILE["display_name"], IDENTITY["agent_id"],
                         IDENTITY["society_origin"], str(self.store.root)):
            self.assertIn(expected, message)
        for secret in ("offline-key-fixture", "invite_token", "Bearer", "agent-notes"):
            self.assertNotIn(secret, message)

    def test_confirmed_rename_receipt_preserves_uuid_and_is_not_repeated(self) -> None:
        receipt = persist_confirmed_rename(self.store, confirmed_display_name="Gamma")
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt.old_display_name, PROFILE["display_name"])
        self.assertEqual(receipt.display_name, "Gamma")
        self.assertEqual(receipt.agent_id, IDENTITY["agent_id"])
        self.assertIn(f"{PROFILE['display_name']} -> Gamma", receipt.message())
        self.assertIn(IDENTITY["agent_id"], receipt.message())
        self.assertNotIn("offline-key-fixture", receipt.message())
        self.assertEqual(self.store.read_identity()["agent_id"], IDENTITY["agent_id"])
        self.assertEqual(self.store.read_profile()["display_name"], "Gamma")
        self.assertIsNone(persist_confirmed_rename(self.store, confirmed_display_name="Gamma"))

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

    def test_noop_only_commits_explicitly_handled_pages(self) -> None:
        self.transport.pages["notices"] = {"items": [{"notice_type": "maintenance"}], "next_cursor": "n1"}
        self.transport.pages["inbox"] = {"items": [{"kind": "reply"}], "next_cursor": "i1"}
        self.transport.pages["thread-updates"] = {"items": [{"thread_id": THREAD_ID}], "next_cursor": "u1"}
        self.transport.pages["feed"] = {"items": [{"thread_id": THREAD_ID}], "next_cursor": "f1"}
        def choose(context):
            context.mark_handled("notices")
            context.mark_handled("inbox")
            context.mark_handled("thread-updates")
            context.mark_handled("feed")
            return WakeChoice()
        outcome = self.wake(choose=choose)
        self.assertEqual(outcome.status, "no_op")
        self.assertEqual([call[1] for call in self.transport.calls if call[0] == "fetch"],
                         ["notices", "inbox", "thread-updates", "feed"])
        state = self.store.read_state()
        self.assertEqual(state["attention_handled"], {"notices": "n1", "inbox": "i1", "thread-updates": "u1"})
        self.assertIsNone(state["social"]["inbox_cursor"])
        self.assertIsNone(state["feed_cursor"])
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
        def choose(context):
            context.mark_handled("inbox")
            return WakeChoice(action=action)
        outcome = self.wake(choose=choose)
        self.assertEqual(outcome.status, "acted")
        self.assertEqual(len([call for call in self.transport.calls if call[0] == "submit"]), 1)
        state = self.store.read_state()
        self.assertEqual(state["attention_handled"]["inbox"], "i1")
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

    def test_noop_run_summary_is_private_bounded_metadata(self) -> None:
        self.transport.pages["inbox"] = {"items": [{"kind": "mention"}], "next_cursor": "i1"}
        def choose(context):
            context.notes.remember("private note body must stay private")
            return WakeChoice()
        result = self.wake(choose=choose)
        self.assertEqual(result.status, "no_op")
        summary = read_recent_runs(self.store)[0]
        self.assertEqual(summary["agent_id"], IDENTITY["agent_id"])
        self.assertEqual(summary["terminal_status"], "success")
        self.assertEqual(summary["observed_counts"]["mentions"], 1)
        self.assertEqual(summary["check_budget_before"], 0)
        self.assertEqual(summary["check_budget_after"], 1)
        self.assertEqual(summary["action_budget_after"], 0)
        self.assertEqual(summary["confirmed_public_actions"], [])
        self.assertEqual(summary["notes_changed"], {"created": 1, "revised": 0, "forgotten": 0})
        run_dir = self.store.root / "runs"
        self.assertEqual(stat.S_IMODE(run_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(next(run_dir.glob("*.json")).stat().st_mode), 0o600)
        raw = next(run_dir.glob("*.json")).read_text()
        for private in ("private note body", "offline-key-fixture", "invite_token", "Bearer"):
            self.assertNotIn(private, raw)

    def test_attention_receipt_records_actual_sources_limits_and_thread_lookup(self) -> None:
        self.transport.pages["feed"] = {
            "items": [{"thread_id": THREAD_ID, "content": "public text must stay out"}],
            "next_cursor": None, "requested_limit": 5,
        }
        self.transport.pages["challenges"] = {
            "items": [{"thread_id": THREAD_ID}] * 2,
            "next_cursor": None, "requested_limit": 10,
        }
        self.transport.pages["agent-commons"] = {
            "items": [{"thread_id": THREAD_ID}] * 3,
            "next_cursor": None, "requested_limit": 10,
        }
        before_identity = self.store.read_identity()
        before_notes = (self.store.root / "agent-notes.json").read_bytes()
        before_config = (self.store.root / "operator-config.json").read_bytes()
        before_approval = (self.store.root / "operator-approval.json").read_bytes()
        def choose(context):
            self.assertEqual(len(context.canonical_thread(THREAD_ID)["posts"]), 1)
            return WakeChoice()  # fetched pages remain unhandled; silence is valid
        outcome = self.wake(
            attention=("feed", "challenges", "agent-commons"), choose=choose,
        )
        self.assertEqual(outcome.status, "no_op")
        summary = read_recent_runs(self.store)[0]
        self.assertEqual(summary["schema_version"], "2")
        self.assertEqual(summary["wake_outcome"], "no_op")
        attention = summary["attention"]
        self.assertEqual(attention["selected_sources"],
                         ["combined_feed", "challenges", "agent_commons", "known_thread"])
        self.assertEqual(attention["fetched_sources"],
                         ["operational_notices", "combined_feed", "challenges", "agent_commons", "known_thread"])
        self.assertEqual(attention["handled_sources"], [])
        self.assertEqual([item["source"] for item in attention["fetches"]],
                         ["operational_notices", "combined_feed", "challenges", "agent_commons"])
        observed = {item["source"]: item for item in attention["fetches"]}
        self.assertEqual((observed["combined_feed"]["view"], observed["combined_feed"]["requested_limit"],
                          observed["combined_feed"]["items_returned"]), ("/api/v1/feed", 5, 1))
        self.assertEqual((observed["challenges"]["view"], observed["challenges"]["requested_limit"],
                          observed["challenges"]["items_returned"]), ("/api/v1/feed?space=challenges", 10, 2))
        self.assertEqual((observed["agent_commons"]["requested_limit"],
                          observed["agent_commons"]["items_returned"]), (10, 3))
        self.assertIsNone(observed["operational_notices"]["requested_limit"])
        self.assertEqual(attention["thread_lookups"][0]["thread_id"], THREAD_ID)
        self.assertEqual(attention["thread_lookups"][0]["posts_returned"], 1)
        self.assertEqual(summary["confirmed_public_actions"], [])
        self.assertEqual(self.store.read_identity(), before_identity)
        self.assertEqual((self.store.root / "agent-notes.json").read_bytes(), before_notes)
        self.assertEqual((self.store.root / "operator-config.json").read_bytes(), before_config)
        self.assertEqual((self.store.root / "operator-approval.json").read_bytes(), before_approval)
        self.assertNotIn("attention", self.store.read_state())
        raw = next((self.store.root / "runs").glob("*.json")).read_text()
        for private in ("public text must stay out", "Canonical text", "private note body",
                        "Bearer", "invite_token", "genesis-50", "offline-key-fixture"):
            self.assertNotIn(private, raw)
        self.assertEqual([call[1] for call in self.transport.calls if call[0] == "fetch"],
                         ["notices", "feed", "challenges", "agent-commons"])

    def test_confirmed_rename_inside_wake_has_receipt_and_journal_event(self) -> None:
        receipts = []
        def choose(context):
            receipts.append(context.persist_confirmed_rename("Gamma"))
            return WakeChoice()
        self.assertEqual(self.wake(choose=choose).status, "no_op")
        self.assertEqual(receipts[0].agent_id, IDENTITY["agent_id"])
        self.assertEqual(self.store.read_profile()["display_name"], "Gamma")
        summary = read_recent_runs(self.store)[0]
        self.assertEqual(summary["confirmed_public_actions"], [
            {"kind": "rename", "thread_id": None, "record_id": None, "parent_post_id": None},
        ])
        self.assertEqual(summary["action_budget_after"], 0)

    def test_run_journal_rejects_unstructured_sensitive_metadata(self) -> None:
        self.assertEqual(self.wake().status, "no_op")
        summary = read_recent_runs(self.store)[0]
        summary["run_id"] = "99999999-9999-4999-8999-999999999999"
        summary["next_attention"] = "Bearer synthetic-secret"
        with self.assertRaises(StateValidationError):
            write_run_summary(self.store, summary)
        self.assertEqual(len(read_recent_runs(self.store)), 1)

    def test_confirmed_post_and_reply_run_summaries(self) -> None:
        self.transport.submit = lambda _action: {"post_id": "88888888-8888-4888-8888-888888888888"}
        post = PublicAction("post", thread_id=THREAD_ID, content="private action draft", runtime_snapshot_id=SNAPSHOT_ID)
        reply = PublicAction("reply", THREAD_ID, POST_ID, "another private draft", SNAPSHOT_ID)
        self.assertEqual(self.wake(choose=lambda _: WakeChoice(action=post)).status, "acted")
        self.assertEqual(self.wake(choose=lambda _: WakeChoice(action=reply), now=NOW + timedelta(seconds=1)).status, "acted")
        summaries = read_recent_runs(self.store)
        self.assertEqual(len(summaries), 2)
        self.assertEqual([item["confirmed_public_actions"][0]["kind"] for item in summaries], ["reply", "post"])
        self.assertTrue(all(item["confirmed_public_actions"][0]["thread_id"] == THREAD_ID for item in summaries))
        self.assertEqual(summaries[0]["action_budget_after"], 2)
        self.assertEqual(summaries[0]["confirmed_public_actions"][0]["parent_post_id"], POST_ID)
        self.assertIsNone(summaries[1]["confirmed_public_actions"][0]["parent_post_id"])
        self.assertNotIn("private action draft", str(summaries))
        self.assertNotIn("another private draft", str(summaries))
        outcomes = [item for item in pending_events(self.store) if item["payload"]["event_type"] == "run_outcome"]
        self.assertEqual({item["runtime_snapshot_id"] for item in outcomes}, {SNAPSHOT_ID})
        self.assertEqual([item["payload"]["outcome"] for item in outcomes], ["post_created", "reply_created"])
        self.assertEqual(outcomes[-1]["payload"]["parent_post_id"], POST_ID)
        self.assertNotIn("private draft", str(outcomes))

    def test_safe_stop_and_ambiguous_write_are_distinct_in_journal(self) -> None:
        self.assertEqual(self.wake(allow_check=lambda _: False).status, "stopped")
        self.assertEqual(read_recent_runs(self.store)[0]["terminal_status"], "safe-stop")
        self.transport.submit_error = TimeoutError("do not store this exception text")
        with self.assertRaises(TimeoutError):
            self.wake(choose=lambda _: WakeChoice(action=PublicAction("reply", THREAD_ID, POST_ID, "draft", SNAPSHOT_ID)))
        summary = read_recent_runs(self.store)[0]
        self.assertEqual(summary["terminal_status"], "pending-reconciliation")
        self.assertTrue(summary["pending_public_write"])
        self.assertEqual(summary["next_attention"], "reconcile_pending_write")
        self.assertEqual(summary["confirmed_public_actions"], [])
        self.assertNotIn("do not store this exception text", str(summary))

    def test_journal_survives_restart_and_failure_does_not_change_identity(self) -> None:
        self.assertEqual(self.wake().status, "no_op")
        restarted = LocalStateStore(self.store.root, expected_agent_id=IDENTITY["agent_id"])
        self.assertEqual(len(read_recent_runs(restarted)), 1)
        with patch("client.mas_client.agent_lifecycle.write_run_summary", side_effect=OSError("disk failure")):
            outcome = self.wake(now=NOW + timedelta(seconds=1))
        self.assertEqual(outcome.status, "no_op")
        self.assertEqual(outcome.journal_error, "OSError")
        self.assertEqual(restarted.read_identity()["agent_id"], IDENTITY["agent_id"])
        self.assertEqual(len(read_recent_runs(restarted)), 1)

    def test_autonomous_thread_post_reply_never_request_per_action_approval(self) -> None:
        requested = []
        actions = (
            PublicAction("thread", title="Autonomous question"),
            PublicAction("post", thread_id=THREAD_ID, content="Autonomous Post", runtime_snapshot_id=SNAPSHOT_ID),
            PublicAction("reply", THREAD_ID, POST_ID, "Autonomous Reply", SNAPSHOT_ID),
        )
        for offset, action in enumerate(actions):
            result = self.wake(
                choose=lambda _context, selected=action: WakeChoice(action=selected),
                approve_public_action=lambda selected: requested.append(selected) or False,
                now=NOW + timedelta(seconds=offset),
            )
            self.assertEqual(result.status, "acted")
        self.assertEqual(requested, [])
        self.assertEqual([call[1].kind for call in self.transport.calls if call[0] == "submit"],
                         ["thread", "post", "reply"])

    def test_supervised_action_requires_explicit_per_action_approval(self) -> None:
        config = approved_config()
        config["public_actions"] = {"mode": "supervised"}
        save_approved_operator_config(self.store, config, approved_at="2026-09-24T00:00:00Z",
                                      approval_reference="supervised-public-action-mode")
        action = PublicAction("post", thread_id=THREAD_ID, content="Supervised Post", runtime_snapshot_id=SNAPSHOT_ID)
        stopped = self.wake(choose=lambda _: WakeChoice(action=action))
        self.assertEqual((stopped.status, stopped.reason), ("stopped", "per_action_approval_required"))
        self.assertIsNone(self.store.read_state()["pending_public_action"])
        self.assertFalse(any(call[0] == "submit" for call in self.transport.calls))
        approved = self.wake(
            choose=lambda _: WakeChoice(action=action),
            approve_public_action=lambda selected: selected is action,
            now=NOW + timedelta(seconds=1),
        )
        self.assertEqual(approved.status, "acted")

    def test_interactive_invocation_does_not_imply_supervised_mode(self) -> None:
        requested = []
        action = PublicAction("thread", title="Interactive but autonomous")
        result = self.wake(
            choose=lambda _: WakeChoice(action=action),
            approve_public_action=lambda selected: requested.append(selected) or False,
            invocation_mode="human_triggered",
        )
        self.assertEqual(result.status, "acted")
        self.assertEqual(requested, [])
        self.assertEqual(read_recent_runs(self.store)[0]["public_action_mode"], "autonomous")

    def test_unauthorized_tool_or_resource_expansion_requires_operator_authorization(self) -> None:
        approvals = []
        for offset, action in enumerate((
            PublicAction("thread", title="Needs web", required_tools=("web_search",)),
            PublicAction("thread", title="Needs paid scope", required_resource_scopes=("new_paid_resource",)),
        )):
            result = self.wake(
                choose=lambda _context, selected=action: WakeChoice(action=selected),
                approve_public_action=lambda selected: approvals.append(selected) or True,
                now=NOW + timedelta(seconds=offset),
            )
            self.assertEqual((result.status, result.reason), ("stopped", "operator_authorization_required"))
        self.assertEqual(approvals, [])
        self.assertFalse(any(call[0] == "submit" for call in self.transport.calls))

    def test_legacy_config_is_not_silently_autonomous_and_can_be_explicitly_updated(self) -> None:
        legacy = approved_config()
        legacy.pop("public_actions")
        before_identity = copy.deepcopy(self.store.read_identity())
        save_approved_operator_config(self.store, legacy, approved_at="2026-09-24T00:00:00Z",
                                      approval_reference="legacy-v04-without-public-action-authority")
        action = PublicAction("thread", title="Must wait for explicit authority")
        blocked = self.wake(choose=lambda _: WakeChoice(action=action))
        self.assertEqual((blocked.status, blocked.reason),
                         ("stopped", "public_action_authorization_required"))
        self.assertEqual(read_recent_runs(self.store)[0]["public_action_mode"], "legacy_unset")
        self.assertEqual(self.wake(now=NOW + timedelta(seconds=1)).status, "no_op")

        updated = copy.deepcopy(legacy)
        updated["public_actions"] = {"mode": "autonomous"}
        updated_identity = copy.deepcopy(before_identity)
        updated_identity["operator_config_id"] = "99999999-9999-4999-8999-999999999999"  # offline fixture for a new server config
        self.store.update_identity(updated_identity)
        save_approved_operator_config(self.store, updated, approved_at="2026-09-25T00:00:02Z",
                                      approval_reference="explicit-autonomous-mode-approval")
        self.assertEqual(self.wake(choose=lambda _: WakeChoice(action=action),
                                   now=NOW + timedelta(seconds=2)).status, "acted")
        after_identity = self.store.read_identity()
        self.assertEqual(after_identity["agent_id"], before_identity["agent_id"])
        self.assertEqual(after_identity["registration"], before_identity["registration"])

    def test_noop_records_supervised_mode_without_requesting_action_approval(self) -> None:
        config = approved_config()
        config["public_actions"] = {"mode": "supervised"}
        save_approved_operator_config(self.store, config, approved_at="2026-09-24T00:00:00Z",
                                      approval_reference="supervised-noop-mode")
        requested = []
        self.assertEqual(self.wake(approve_public_action=lambda action: requested.append(action) or True).status,
                         "no_op")
        self.assertEqual(requested, [])
        summary = read_recent_runs(self.store)[0]
        self.assertEqual(summary["public_action_mode"], "supervised")
        self.assertEqual(summary["confirmed_public_actions"], [])

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
            for local, agent_id in ((a, first), (b, second)):
                outcome = run_wake(local, transport=FakeTransport(),
                                   control=lambda: ControlDecision(True, True, True, source="live"),
                                   choose=lambda _: WakeChoice(), expected_agent_id=agent_id, now=NOW)
                self.assertEqual(outcome.status, "no_op")
                self.assertEqual(read_recent_runs(local)[0]["agent_id"], agent_id)
            self.assertNotEqual(read_recent_runs(a)[0]["run_id"], read_recent_runs(b)[0]["run_id"])
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

    def test_selected_source_paths_use_existing_filtered_feed_and_self_endpoints(self) -> None:
        self.assertEqual(attention_page_path("challenges", limit=20), "/api/v1/feed?space=challenges&limit=20")
        self.assertEqual(attention_page_path("world-pulse", limit=7), "/api/v1/feed?space=world-pulse&limit=7")
        self.assertEqual(attention_page_path("agent-commons"), "/api/v1/feed?space=agent-commons")
        self.assertEqual(attention_page_path("feed", cursor="abc"), "/api/v1/feed?cursor=abc")
        self.assertEqual(attention_page_path("own-threads"), "/api/v1/me/threads")
        self.assertEqual(attention_page_path("own-posts"), "/api/v1/me/posts")
        with self.assertRaises(ValueError):
            attention_page_path("own-idea")

    def test_new_inbox_item_after_acknowledged_page_is_visible_next_wake(self) -> None:
        self.transport.pages["inbox"] = {"items": [{"kind": "reply", "post_id": "old"}], "next_cursor": "old-cursor"}
        def choose(context):
            context.mark_handled("inbox")
            return WakeChoice()
        self.assertEqual(self.wake(attention=("inbox",), choose=choose).status, "no_op")
        self.transport.pages["inbox"] = {"items": [{"kind": "reply", "post_id": "new"}], "next_cursor": "new-cursor"}
        self.assertEqual(self.wake(attention=("inbox",), choose=choose, now=NOW + timedelta(seconds=1)).status, "no_op")
        calls = [c for c in self.transport.calls if c[0] == "fetch" and c[1] == "inbox"]
        self.assertEqual([c[2] for c in calls], [None, "old-cursor"])
        self.assertEqual(self.store.read_state()["attention_handled"]["inbox"], "new-cursor")

    def test_all_sources_can_be_selected_without_a_space_quota(self) -> None:
        from client.mas_client.agent_lifecycle import SOCIAL_SOURCES
        self.assertEqual(self.wake(attention=SOCIAL_SOURCES).status, "no_op")
        fetched = [c[1] for c in self.transport.calls if c[0] == "fetch"]
        self.assertEqual(fetched, ["notices", *[s for s in SOCIAL_SOURCES if s not in {"known-thread", "own-idea"}]])
        self.assertEqual(read_recent_runs(self.store)[0]["selected_attention"], list(SOCIAL_SOURCES))
        self.assertEqual(self.store.read_state()["rolling_action_timestamps"], [])

    def test_known_thread_and_own_history_are_optional(self) -> None:
        def choose(context):
            self.assertEqual(context.fetch_source("own-posts"), [])
            self.assertEqual(context.canonical_thread(THREAD_ID)["thread_id"], THREAD_ID)
            context.mark_handled("known-thread")
            return WakeChoice()
        self.assertEqual(self.wake(attention=("own-threads",), choose=choose).status, "no_op")
        self.assertEqual([c[1] for c in self.transport.calls if c[0] == "fetch"],
                         ["notices", "own-threads", "own-posts"])
        self.assertEqual(read_recent_runs(self.store)[0]["handled_attention"], ["known-thread"])
        self.assertEqual(self.store.read_state()["rolling_action_timestamps"], [])

    def test_unfetched_source_cannot_be_marked_handled(self) -> None:
        def choose(context):
            with self.assertRaises(ValueError):
                context.mark_handled("inbox")
            return WakeChoice()
        self.assertEqual(self.wake(attention=(), choose=choose).status, "no_op")
        self.assertNotIn("attention_handled", self.store.read_state())

    def test_only_selected_inbox_is_fetched_and_handled(self) -> None:
        self.transport.pages["inbox"] = {"items": [{"kind": "mention", "content": "private fixture"}], "next_cursor": "i1"}
        def choose(context):
            self.assertEqual(context.inbox[0]["kind"], "mention")
            context.mark_handled("inbox")
            return WakeChoice()
        result = self.wake(attention=("inbox",), choose=choose)
        self.assertEqual(result.status, "no_op")
        self.assertEqual([call[1] for call in self.transport.calls if call[0] == "fetch"], ["notices", "inbox"])
        self.assertEqual(self.store.read_state()["attention_handled"],
                         {"notices": None, "inbox": "i1", "thread-updates": None})
        summary = read_recent_runs(self.store)[0]
        self.assertEqual(summary["selected_attention"], ["inbox"])
        self.assertEqual(summary["fetched_attention"], ["notices", "inbox"])
        self.assertEqual(summary["handled_attention"], ["inbox"])
        self.assertNotIn("private fixture", str(summary))

    def test_only_challenges_can_be_selected_without_other_social_fetches(self) -> None:
        self.transport.pages["challenges"] = {"items": [{"thread_id": THREAD_ID}], "next_cursor": None}
        def choose(context):
            self.assertEqual(context.source_pages["challenges"][0]["thread_id"], THREAD_ID)
            context.mark_handled("challenges")
            return WakeChoice(stop_early=True)
        self.assertEqual(self.wake(attention=("challenges",), choose=choose).status, "no_op")
        self.assertEqual([c[1] for c in self.transport.calls if c[0] == "fetch"], ["notices", "challenges"])
        self.assertNotIn("attention_handled", self.store.read_state())
        summary = read_recent_runs(self.store)[0]
        self.assertEqual(summary["handled_attention"], ["challenges"])
        self.assertTrue(summary["stopped_early"])

    def test_combined_and_filtered_sources_are_independent_optional_views(self) -> None:
        self.transport.pages["feed"] = {"items": [{"thread_id": THREAD_ID}], "next_cursor": None}
        self.transport.pages["world-pulse"] = {"items": [], "next_cursor": None}
        self.transport.pages["agent-commons"] = {"items": [], "next_cursor": None}
        def choose(context):
            self.assertIn("own-idea", context.available_attention_sources)
            self.assertEqual(context.fetch_source("agent-commons"), [])
            context.mark_handled("feed")
            return WakeChoice()
        self.assertEqual(self.wake(attention=("feed", "world-pulse"), choose=choose).status, "no_op")
        self.assertEqual([c[1] for c in self.transport.calls if c[0] == "fetch"],
                         ["notices", "feed", "world-pulse", "agent-commons"])
        self.assertEqual(read_recent_runs(self.store)[0]["selected_attention"],
                         ["feed", "world-pulse", "agent-commons"])
        self.assertIsNone(self.store.read_state()["feed_cursor"])

    def test_no_social_source_is_mandatory_and_noop_stops_early(self) -> None:
        result = run_wake(self.store, transport=self.transport,
                          control=lambda: ControlDecision(True, True, True, source="live"),
                          choose=lambda context: WakeChoice(),
                          expected_agent_id=IDENTITY["agent_id"], now=NOW)
        self.assertEqual(result.status, "no_op")
        self.assertEqual([c[1] for c in self.transport.calls if c[0] == "fetch"], ["notices"])
        self.assertEqual(read_recent_runs(self.store)[0]["selected_attention"], [])
        self.assertTrue(read_recent_runs(self.store)[0]["stopped_early"])
        self.assertEqual(self.store.read_state()["rolling_action_timestamps"], [])

    def test_self_initiated_thread_needs_no_feed_or_incoming_item(self) -> None:
        action = PublicAction("thread", title="A question the Agent chose")
        result = self.wake(attention=("own-idea",), choose=lambda _: WakeChoice(action=action))
        self.assertEqual(result.status, "acted")
        self.assertEqual([c[1] for c in self.transport.calls if c[0] == "fetch"], ["notices"])
        self.assertEqual([c[1].kind for c in self.transport.calls if c[0] == "submit"], ["thread"])
        self.assertEqual(read_recent_runs(self.store)[0]["selected_attention"], ["own-idea"])

    def test_fetch_is_not_acknowledgment_even_for_mention_or_operational_notice(self) -> None:
        self.transport.pages["notices"] = {"items": [{"notice_type": "maintenance"}], "next_cursor": "n1"}
        self.transport.pages["inbox"] = {"items": [{"kind": "mention"}], "next_cursor": "i1"}
        self.assertEqual(self.wake(attention=("inbox",), choose=lambda _: WakeChoice(stop_early=True)).status, "no_op")
        self.assertNotIn("attention_handled", self.store.read_state())
        self.assertEqual(self.store.read_state()["rolling_action_timestamps"], [])
        self.assertEqual(read_recent_runs(self.store)[0]["handled_attention"], [])

    def test_concurrent_handled_marker_cannot_be_moved_backward(self) -> None:
        self.transport.pages["inbox"] = {"items": [{"kind": "reply"}], "next_cursor": "older"}
        def choose(context):
            context.mark_handled("inbox")
            def concurrent(state):
                state["attention_handled"] = {"notices": None, "inbox": "newer", "thread-updates": None}
                return state, None
            self.store.update_state(concurrent)
            return WakeChoice()
        with self.assertRaises(StateValidationError):
            self.wake(attention=("inbox",), choose=choose)
        self.assertEqual(self.store.read_state()["attention_handled"]["inbox"], "newer")

    def test_later_fetched_page_is_not_implicitly_acknowledged(self) -> None:
        original = self.transport.fetch_page
        def fetch(stream, cursor):
            if stream == "inbox":
                self.transport.calls.append(("fetch", stream, cursor))
                return ({"items": [{"kind": "reply", "post_id": "first"}], "next_cursor": "first"} if cursor is None
                        else {"items": [{"kind": "reply", "post_id": "second"}], "next_cursor": "second"})
            return original(stream, cursor)
        self.transport.fetch_page = fetch
        def choose(context):
            context.mark_handled("inbox")
            context.fetch_more("inbox")
            return WakeChoice(stop_early=True)
        self.assertEqual(self.wake(attention=("inbox",), choose=choose).status, "no_op")
        self.assertEqual(self.store.read_state()["attention_handled"]["inbox"], "first")
        summary = read_recent_runs(self.store)[0]
        self.assertEqual(summary["observed_counts"]["inbox"], 2)
        inbox_pages = [item for item in summary["attention"]["fetches"] if item["source"] == "inbox"]
        self.assertEqual([item["handled"] for item in inbox_pages], [True, False])
        self.assertEqual(summary["attention"]["handled_sources"], ["inbox"])

    def test_feed_cursor_is_page_local_and_new_activity_appears_next_wake(self) -> None:
        self.transport.pages["feed"] = {"items": [{"thread_id": "old"}], "next_cursor": None}
        seen = []
        def choose(context):
            seen.append(context.feed[0]["thread_id"])
            context.mark_handled("feed")
            return WakeChoice()
        self.assertEqual(self.wake(attention=("feed",), choose=choose).status, "no_op")
        self.transport.pages["feed"] = {"items": [{"thread_id": "new"}, {"thread_id": "old"}], "next_cursor": None}
        self.assertEqual(self.wake(attention=("feed",), choose=choose, now=NOW + timedelta(seconds=1)).status, "no_op")
        self.assertEqual(seen, ["old", "new"])
        feed_calls = [c for c in self.transport.calls if c[0] == "fetch" and c[1] == "feed"]
        self.assertEqual([c[2] for c in feed_calls], [None, None])
        self.assertIsNone(self.store.read_state()["feed_cursor"])
        self.assertEqual(self.store.read_state()["rolling_action_timestamps"], [])

    def test_same_feed_page_can_be_reread_without_automatic_processing(self) -> None:
        self.transport.pages["feed"] = {"items": [{"thread_id": THREAD_ID}], "next_cursor": None}
        for offset in (0, 1):
            self.assertEqual(self.wake(attention=("feed",), now=NOW + timedelta(seconds=offset)).status, "no_op")
        self.assertEqual([c[2] for c in self.transport.calls if c[0] == "fetch" and c[1] == "feed"],
                         [None, None])
        self.assertEqual(len(self.store.read_state()["rolling_check_timestamps"]), 2)
        self.assertEqual(self.store.read_state()["rolling_action_timestamps"], [])
        self.assertEqual([r["confirmed_public_actions"] for r in read_recent_runs(self.store)], [[], []])

    def test_control_failure_prevents_attention_selection_and_social_fetch(self) -> None:
        selected = []
        result = run_wake(self.store, transport=self.transport,
                          control=lambda: ControlDecision(False, False, False, source="live", stop_reason="maintenance"),
                          choose_attention=lambda _: selected.append(True) or ("feed",),
                          choose=lambda _: self.fail("decision after denied control"),
                          expected_agent_id=IDENTITY["agent_id"], now=NOW)
        self.assertEqual((result.status, result.reason), ("stopped", "maintenance"))
        self.assertEqual(selected, [])
        self.assertFalse(any(c[0] == "fetch" for c in self.transport.calls))

    def test_feed_can_page_older_within_wake_without_persisting_cursor(self) -> None:
        original = self.transport.fetch_page
        def fetch(stream, cursor):
            if stream == "feed":
                self.transport.calls.append(("fetch", stream, cursor))
                return ({"items": [{"thread_id": "recent"}], "next_cursor": "older"} if cursor is None
                        else {"items": [{"thread_id": "older"}], "next_cursor": None})
            return original(stream, cursor)
        self.transport.fetch_page = fetch
        def choose(context):
            self.assertEqual(context.fetch_more("feed"), [{"thread_id": "older"}])
            self.assertEqual(context.source_pages["feed"],
                             [{"thread_id": "recent"}, {"thread_id": "older"}])
            context.mark_handled("feed")
            return WakeChoice()
        self.assertEqual(self.wake(attention=("feed",), choose=choose).status, "no_op")
        self.assertEqual([c[2] for c in self.transport.calls if c[0] == "fetch" and c[1] == "feed"], [None, "older"])
        self.assertIsNone(self.store.read_state()["feed_cursor"])
        attention = read_recent_runs(self.store)[0]["attention"]
        pages = [item for item in attention["fetches"] if item["source"] == "combined_feed"]
        self.assertEqual([item["pages_fetched"] for item in pages], [1, 2])
        self.assertEqual([item["items_returned"] for item in pages], [1, 1])
        self.assertEqual([item["pagination_used"] for item in pages], [False, True])
        self.assertEqual([item["next_cursor_present"] for item in pages], [True, False])
        self.assertEqual([item["requested_limit"] for item in pages], [None, None])
        self.assertEqual(attention["handled_sources"], ["combined_feed"])

    def test_legacy_cursors_are_preserved_but_not_trusted_as_handled(self) -> None:
        original_identity = self.store.read_identity()
        original_notes = (self.store.root / "agent-notes.json").read_bytes()
        state = self.store.read_state()
        state["feed_cursor"] = "legacy-older-page"
        state["social"]["inbox_cursor"] = "legacy-auto-advanced"
        state["social"]["participated_threads_cursor"] = "legacy-auto-advanced"
        self.store.write_state(state)
        self.transport.pages["inbox"] = {"items": [{"kind": "reply"}], "next_cursor": "first-real-handled"}
        self.transport.pages["feed"] = {"items": [{"thread_id": THREAD_ID}], "next_cursor": None}
        self.assertEqual(self.wake(attention=("inbox", "feed"), now=NOW).status, "no_op")
        self.assertEqual([c[2] for c in self.transport.calls if c[0] == "fetch" and c[1] in {"inbox", "feed"}], [None, None])
        after = self.store.read_state()
        self.assertNotIn("attention_handled", after)
        self.assertEqual(after["feed_cursor"], "legacy-older-page")
        self.assertEqual(after["social"]["inbox_cursor"], "legacy-auto-advanced")
        self.assertEqual(after["rolling_action_timestamps"], state["rolling_action_timestamps"])
        self.assertEqual(self.store.read_identity(), original_identity)
        self.assertEqual((self.store.root / "agent-notes.json").read_bytes(), original_notes)

if __name__ == "__main__":
    unittest.main()
