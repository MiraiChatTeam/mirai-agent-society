import copy
import tempfile
import unittest
from pathlib import Path

from client.mas_client.local_state import LocalStateStore, StateValidationError
from client.mas_client.social_state import empty_social_state, update_social_state
from client.tests.test_local_state import IDENTITY, STATE


class SocialStateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = LocalStateStore(Path(temporary.name) / ".mas")
        self.store.provision_identity(copy.deepcopy(IDENTITY))
        self.store.write_state(copy.deepcopy(STATE))

    def test_bounded_roundtrip_preserves_identity(self):
        social = empty_social_state()
        social["inbox_cursor"] = "inbox-page-1"
        social["own_activity_cursor"] = "own-page-1"
        social["participated_threads_cursor"] = "thread-page-1"
        social["memory"] = {
            "updated_at": "2026-09-23T12:00:00Z",
            "window_start": "2026-09-20T00:00:00Z",
            "summary": "Discussion still open; reread canonical Thread before answering.",
            "active_threads": [{
                "thread_id": "11111111-1111-4111-8111-111111111111",
                "last_seen_post_id": None,
                "note": "Follow-up pending",
            }],
        }
        update_social_state(self.store, social)
        restarted = LocalStateStore(self.store.root)
        self.assertEqual(restarted.read_state()["social"], social)
        self.assertEqual(restarted.read_identity(), IDENTITY)

    def test_loss_or_corruption_of_social_memory_does_not_change_identity(self):
        state = self.store.read_state()
        state.pop("social")  # older valid v1 state, or explicitly discarded memory
        self.store.write_state(state)
        self.assertNotIn("social", self.store.read_state())
        self.assertEqual(self.store.read_identity(), IDENTITY)
        update_social_state(self.store, empty_social_state())
        self.assertEqual(self.store.read_identity(), IDENTITY)

    def test_summary_and_active_thread_bounds(self):
        social = empty_social_state()
        social["memory"]["summary"] = "x" * 4097
        with self.assertRaises(StateValidationError):
            update_social_state(self.store, social)
        social = empty_social_state()
        social["memory"]["active_threads"] = [{
            "thread_id": "11111111-1111-4111-8111-111111111111",
            "last_seen_post_id": None, "note": ""
        }] * 33
        with self.assertRaises(StateValidationError):
            update_social_state(self.store, social)
        self.assertEqual(self.store.read_identity(), IDENTITY)


if __name__ == "__main__":
    unittest.main()
