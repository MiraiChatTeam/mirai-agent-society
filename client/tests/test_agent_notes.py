"""M7.5 local, private Agent memory contract tests."""

import copy
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from client.mas_client.agent_notes import AgentNotesStore
from client.mas_client.local_state import (
    IdentityConflictError, IdentityMissingError, LocalStateStore, StateValidationError,
)
from client.mas_client.social_state import empty_social_state, update_social_state
from client.tests.test_local_state import IDENTITY, PROFILE, STATE


AGENT_REF = {"type": "agent", "id": "44444444-4444-4444-8444-444444444444"}
THREAD_REF = {"type": "thread", "id": "55555555-5555-4555-8555-555555555555"}


class AgentNotesTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / ".mas"
        self.local = LocalStateStore(self.root)
        self.local.provision_identity(copy.deepcopy(IDENTITY))
        self.notes = AgentNotesStore(self.local)

    def test_missing_file_becomes_empty_private_memory_without_new_identity(self) -> None:
        self.assertFalse((self.root / "agent-notes.json").exists())
        self.assertEqual(self.notes.list_recent(), [])
        document = json.loads((self.root / "agent-notes.json").read_text(encoding="utf-8"))
        self.assertEqual(document["schema_version"], "1")
        self.assertEqual(document["agent_id"], IDENTITY["agent_id"])
        self.assertEqual(document["notes"], [])
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.root / "agent-notes.json").stat().st_mode), 0o600)
        self.assertEqual(self.local.read_identity(), IDENTITY)

    def test_remember_get_selective_search_and_rename_uuid_reference(self) -> None:
        self.local.write_profile(copy.deepcopy(PROFILE))
        first = self.notes.remember(
            "Nova raised a useful objection.", kind="agent", refs=[AGENT_REF, THREAD_REF],
            tags=["unresolved", "physics"],
        )
        second = self.notes.remember("Revisit a different question.", kind="topic", tags=["biology"])
        self.assertEqual(self.notes.get(first["note_id"]), first)
        self.assertEqual(self.notes.search(ref=AGENT_REF), [first])
        self.assertEqual(self.notes.search(ref=THREAD_REF), [first])
        self.assertEqual(self.notes.search(query="OBJECTION"), [first])
        self.assertEqual(self.notes.search(tag="PHYSICS"), [first])
        self.assertEqual(self.notes.search(query="biology"), [second])
        self.assertEqual(len(self.notes.list_recent(limit=1)), 1)
        renamed = copy.deepcopy(PROFILE)
        renamed["display_name"] = "Gamma"
        self.local.write_profile(renamed)
        restarted = AgentNotesStore(LocalStateStore(self.root))
        self.assertEqual(restarted.search(ref=AGENT_REF)[0]["note_id"], first["note_id"])
        self.assertEqual(restarted.local.read_identity()["agent_id"], IDENTITY["agent_id"])

    def test_revise_forget_and_merge_are_agent_authored_and_atomic(self) -> None:
        first = self.notes.remember("Initial thought.", kind="self", refs=[AGENT_REF], tags=["idea"])
        second = self.notes.remember("Another thought.", kind="thread", refs=[THREAD_REF], tags=["followup"])
        revised = self.notes.revise(first["note_id"], text="I changed my view.", tags=["revised"])
        self.assertEqual(revised["note_id"], first["note_id"])
        self.assertEqual(self.notes.get(first["note_id"])["text"], "I changed my view.")
        merged = self.notes.merge(
            [first["note_id"], second["note_id"]], text="My own synthesis.", kind="topic"
        )
        self.assertEqual(merged["text"], "My own synthesis.")
        self.assertEqual(merged["kind"], "topic")
        self.assertEqual(merged["refs"], [AGENT_REF, THREAD_REF])
        self.assertEqual(self.notes.get(first["note_id"]), None)
        self.assertEqual(self.notes.get(second["note_id"]), None)
        self.assertTrue(self.notes.forget(merged["note_id"]))
        self.assertFalse(self.notes.delete(merged["note_id"]))
        self.assertEqual(self.notes.list_recent(), [])

    def test_corrupt_or_wrong_owner_is_not_silently_overwritten(self) -> None:
        self.notes.list_recent()
        path = self.root / "agent-notes.json"
        path.write_text("{broken", encoding="utf-8")
        original = path.read_bytes()
        with self.assertRaises(StateValidationError):
            self.notes.remember("Must not overwrite corruption.")
        self.assertEqual(path.read_bytes(), original)
        wrong = {"schema_version": "1", "agent_id": AGENT_REF["id"],
                 "updated_at": "2026-09-25T00:00:00Z", "notes": []}
        path.write_text(json.dumps(wrong), encoding="utf-8")
        with self.assertRaises(IdentityConflictError):
            self.notes.list_recent()
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), wrong)
        self.assertEqual(self.local.read_identity(), IDENTITY)

    def test_atomic_replace_failure_keeps_previous_notes(self) -> None:
        note = self.notes.remember("Original note.")
        path = self.root / "agent-notes.json"
        before = path.read_bytes()
        with patch("client.mas_client.local_state.os.replace", side_effect=OSError("simulated crash")):
            with self.assertRaises(OSError):
                self.notes.revise(note["note_id"], text="Uncommitted revision.")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(self.root.glob(".notes.*.tmp")), [])

    def test_sensitive_material_and_bad_refs_never_persist(self) -> None:
        self.notes.list_recent()
        path = self.root / "agent-notes.json"
        before = path.read_bytes()
        for text in ("api_key=secret-value", "Bearer example-token", "-----BEGIN PRIVATE KEY-----", "/home/private/file"):
            with self.subTest(text=text), self.assertRaises(StateValidationError):
                self.notes.remember(text)
        with self.assertRaises(StateValidationError):
            self.notes.remember("Invalid Agent reference", refs=[{"type": "agent", "id": "Nova"}])
        with self.assertRaises(StateValidationError):
            self.notes.remember("Unsafe reference", refs=[{"type": "source", "id": "https://site.test/?api_key=secret"}])
        self.assertEqual(path.read_bytes(), before)
        self.assertNotIn(b"secret-value", path.read_bytes())

    def test_notes_permissions_and_identity_loss_fail_safe(self) -> None:
        self.notes.list_recent()
        path = self.root / "agent-notes.json"
        os.chmod(path, 0o644)
        with self.assertRaises(StateValidationError):
            self.notes.list_recent()
        os.chmod(path, 0o600)
        (self.root / "identity.json").unlink()
        with self.assertRaises(IdentityMissingError):
            self.notes.remember("Do not create another identity.")

    def test_memory_loss_and_working_memory_bounds_do_not_change_identity(self) -> None:
        self.local.write_state(copy.deepcopy(STATE))
        self.notes.remember("Disposable subjective memory.")
        (self.root / "agent-notes.json").unlink()
        self.assertEqual(self.notes.list_recent(), [])
        self.assertEqual(self.local.read_identity(), IDENTITY)
        social = empty_social_state()
        social["memory"]["summary"] = "x" * 4097
        with self.assertRaises(StateValidationError):
            update_social_state(self.local, social)
        self.assertEqual(self.local.read_identity(), IDENTITY)


if __name__ == "__main__":
    unittest.main()
