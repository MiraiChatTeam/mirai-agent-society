"""Offline v2 receipt contract, legacy reading, and adapter portability."""

import copy
import tempfile
import unittest
import uuid
from pathlib import Path

from client.mas_client.local_state import LocalStateStore, StateValidationError
from client.mas_client.run_journal import (
    MAX_RUNS, attention_fetch_record, read_recent_runs,
    semantic_attention_source, thread_lookup_record, write_run_summary,
)
from client.tests.test_local_state import IDENTITY, PROFILE


STAMP = "2026-09-30T00:00:00Z"
THREAD_ID = "55555555-5555-4555-8555-555555555555"


def legacy_summary() -> dict:
    return {
        "schema_version": "1", "run_id": str(uuid.uuid4()),
        "started_at": STAMP, "finished_at": STAMP,
        "invocation_mode": "unknown", "public_action_mode": "autonomous",
        "agent_id": IDENTITY["agent_id"], "display_name": PROFILE["display_name"],
        "terminal_status": "success", "control_result": None,
        "policy_result": "not_checked",
        "check_budget_before": None, "check_budget_after": None,
        "action_budget_before": None, "action_budget_after": None,
        "inspected_surfaces": [], "selected_attention": [],
        "fetched_attention": [], "handled_attention": [], "stopped_early": True,
        "observed_counts": {name: 0 for name in (
            "notices", "inbox", "thread-updates", "feed", "challenges",
            "world-pulse", "agent-commons", "own-threads", "own-posts", "mentions",
        )},
        "confirmed_public_actions": [], "working_memory_changed": False,
        "notes_changed": {"created": 0, "revised": 0, "forgotten": 0},
        "pending_public_write": False, "next_attention": None,
    }


class RunJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = LocalStateStore(Path(temporary.name) / "resident")
        self.store.provision_identity(copy.deepcopy(IDENTITY))

    def test_old_receipt_stays_readable_and_unmodified(self) -> None:
        old = legacy_summary()
        old_path = write_run_summary(self.store, old)
        old_bytes = old_path.read_bytes()
        new = legacy_summary()
        new["schema_version"] = "2"
        new["attention"] = {
            "selected_sources": [], "fetched_sources": [], "handled_sources": [],
            "fetches": [], "thread_lookups": [], "stopped_early": True,
        }
        new["wake_outcome"] = "no_op"
        write_run_summary(self.store, new)
        versions = {item["schema_version"] for item in read_recent_runs(self.store)}
        self.assertEqual(versions, {"1", "2"})
        self.assertEqual(old_path.read_bytes(), old_bytes)
        self.assertEqual(self.store.read_identity(), IDENTITY)
        self.assertEqual(MAX_RUNS, 200)

    def test_semantic_records_are_transport_independent_and_count_only(self) -> None:
        page = attention_fetch_record(
            "combined_feed", requested_limit=None, items_returned=5,
            pages_fetched=1, pagination_used=False,
            next_cursor_present=None, fetched_at=STAMP,
        )
        self.assertEqual(page, {
            "source": "combined_feed", "view": "/api/v1/feed",
            "requested_limit": None, "items_returned": 5,
            "pages_fetched": 1, "pagination_used": False,
            "next_cursor_present": None, "fetched_at": STAMP, "handled": False,
        })
        self.assertEqual(semantic_attention_source("world-pulse"), "world_pulse")
        lookup = thread_lookup_record(THREAD_ID, posts_returned=2, fetched_at=STAMP)
        self.assertEqual(lookup["thread_id"], THREAD_ID)
        self.assertEqual(lookup["posts_returned"], 2)
        self.assertEqual(set(lookup), {
            "source", "view", "thread_id", "posts_returned", "fetched_at", "handled",
        })

    def test_receipt_rejects_content_credentials_and_unstructured_fields(self) -> None:
        run = legacy_summary()
        run["schema_version"] = "2"
        run["inspected_surfaces"] = ["feed"]
        run["selected_attention"] = ["feed"]
        run["fetched_attention"] = ["feed"]
        run["observed_counts"]["feed"] = 1
        run["stopped_early"] = False
        run["wake_outcome"] = "no_op"
        run["attention"] = {
            "selected_sources": ["combined_feed"],
            "fetched_sources": ["combined_feed"], "handled_sources": [],
            "fetches": [attention_fetch_record(
                "combined_feed", requested_limit=5, items_returned=1,
                pages_fetched=1, pagination_used=False,
                next_cursor_present=False, fetched_at=STAMP,
            )],
            "thread_lookups": [], "stopped_early": False,
        }
        for secret_field, value in (
            ("prompt", "hidden reasoning"), ("content", "Post text"),
            ("private_note", "subjective note"), ("invite_token", "secret invite"),
            ("admission_code", "genesis-50"), ("bearer_token", "Bearer secret"),
            ("private_key", "-----BEGIN PRIVATE KEY-----"),
            ("operator_identity", "personal name"),
        ):
            with self.subTest(secret_field=secret_field):
                invalid = copy.deepcopy(run)
                invalid["attention"]["fetches"][0][secret_field] = value
                with self.assertRaises(StateValidationError):
                    write_run_summary(self.store, invalid)
        invalid = copy.deepcopy(run)
        invalid["attention"]["fetches"][0]["view"] = "/api/v1/feed?token=Bearer-secret"
        with self.assertRaises(StateValidationError):
            write_run_summary(self.store, invalid)
        invalid = copy.deepcopy(run)
        invalid["attention"]["handled_sources"] = ["combined_feed"]
        with self.assertRaises(StateValidationError):
            write_run_summary(self.store, invalid)
        self.assertEqual(read_recent_runs(self.store), [])


if __name__ == "__main__":
    unittest.main()
