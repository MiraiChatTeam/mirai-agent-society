"""Offline strict projection, per-Agent buffering and retry isolation."""

import copy
import os
import tempfile
import unittest
import uuid
from pathlib import Path

from client.mas_client.local_state import LocalStateStore, StateValidationError
from client.mas_client.research_telemetry import (
    begin_run, fetch_payload, finish_run, flush_pending, handled_payload,
    outcome_payload, pending_events, queue_event, thread_payload,
)
from client.tests.test_local_state import IDENTITY

THREAD_ID = "55555555-5555-4555-8555-555555555555"
POST_ID = "66666666-6666-4666-8666-666666666666"


class BufferTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.store = LocalStateStore.for_agent(IDENTITY["agent_id"], base=self.base)
        self.store.provision_identity(copy.deepcopy(IDENTITY))

    def test_run_identity_survives_crash_then_new_run_after_completion(self):
        first = begin_run(self.store)
        self.assertEqual(first, begin_run(self.store))
        self.assertEqual(uuid.UUID(first).version, 4)
        finish_run(self.store, first)
        self.assertNotEqual(first, begin_run(self.store))
        self.assertEqual(self.store.read_identity(), IDENTITY)

    def test_ordered_public_ids_and_no_content(self):
        other = str(uuid.uuid4())
        payload = fetch_payload("combined_feed", requested_limit=None, page_index=1, items=[
            {"thread_id": THREAD_ID, "title": "Never upload this title"},
            {"thread_id": other, "title": "Another title"},
        ], pagination_used=False, next_cursor_present=None)
        self.assertEqual(payload["returned_thread_ids"], [THREAD_ID, other])
        self.assertIsNone(payload["requested_limit"])
        self.assertNotIn("title", str(payload))
        opened = thread_payload(THREAD_ID, [{"post_id": POST_ID, "content": "Private local copy"}])
        self.assertEqual(opened["returned_post_ids"], [POST_ID])
        self.assertNotIn("content", str(opened))
        self.assertEqual(outcome_payload("no_op")["outcome"], "no_op")

    def test_idempotent_retry_uses_same_event_id_and_private_files(self):
        run_id = begin_run(self.store)
        fetch = fetch_payload("combined_feed", requested_limit=5, page_index=1,
                              items=[{"thread_id": THREAD_ID}], pagination_used=False,
                              next_cursor_present=False)
        fetch_id = queue_event(self.store, run_id, fetch)
        handled_id = queue_event(self.store, run_id, handled_payload("combined_feed", fetch_id))
        events = pending_events(self.store)
        self.assertEqual([item["event_id"] for item in events], [fetch_id, handled_id])
        pending = self.store.root / "telemetry" / "pending"
        self.assertEqual(os.stat(pending).st_mode & 0o777, 0o700)
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in pending.iterdir()))
        class Ambiguous:
            def __init__(self):
                self.calls = []
            def submit_telemetry_batch(self, body):
                self.calls.append(body)
                if len(self.calls) == 1:
                    raise TimeoutError("uncertain response")
                return {"accepted": 0, "duplicates": len(body["events"])}
        transport = Ambiguous()
        with self.assertRaises(TimeoutError):
            flush_pending(self.store, transport)
        self.assertEqual(len(pending_events(self.store)), 2)
        self.assertEqual(flush_pending(self.store, transport), 2)
        self.assertEqual(transport.calls[0], transport.calls[1])
        self.assertEqual(pending_events(self.store), [])

    def test_receipt_or_private_fields_cannot_enter_projection(self):
        run_id = begin_run(self.store)
        good = outcome_payload("no_op")
        for field in ("selected_sources", "prompt", "reasoning", "notes", "operator_identity",
                      "local_path", "bearer_token", "private_key", "admission_code", "content"):
            with self.subTest(field=field):
                with self.assertRaises(StateValidationError):
                    queue_event(self.store, run_id, dict(good, **{field: "secret"}))
        self.assertEqual(pending_events(self.store), [])

    def test_tampered_pending_file_cannot_upload_private_text(self):
        run_id = begin_run(self.store)
        event_id = queue_event(self.store, run_id, outcome_payload("no_op"))
        path = self.store.root / "telemetry" / "pending" / f"{event_id}.json"
        import json
        document = json.loads(path.read_text())
        document["payload"]["prompt"] = "private reasoning"
        path.write_text(json.dumps(document))
        os.chmod(path, 0o600)
        class Upload:
            called = False
            def submit_telemetry_batch(self, body):
                self.called = True
                return {"accepted": len(body["events"]), "duplicates": 0}
        transport = Upload()
        with self.assertRaises(StateValidationError):
            flush_pending(self.store, transport)
        self.assertFalse(transport.called)

    def test_separate_agent_roots_do_not_share_pending_events(self):
        other_id = str(uuid.uuid4())
        other = LocalStateStore.for_agent(other_id, base=self.base)
        identity = copy.deepcopy(IDENTITY)
        identity["agent_id"] = other_id
        other.provision_identity(identity)
        queue_event(self.store, begin_run(self.store), outcome_payload("no_op"))
        self.assertEqual(pending_events(other), [])
        self.assertNotEqual(begin_run(self.store), begin_run(other))
