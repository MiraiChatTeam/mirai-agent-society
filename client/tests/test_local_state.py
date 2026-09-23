import copy
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from client.mas_client.local_state import (
    IdentityConflictError,
    IdentityMissingError,
    LocalStateStore,
    StateMissingError,
    StateValidationError,
    validate_document,
)


IDENTITY = {
    "schema_version": "1",
    "society_origin": "https://mas.example.org",
    "constitution_version": "1",
    "constitution_sha256": "15173811c09fd646c16c392ab8038a2c237763c63e6391f67aa1da1de2d9ddbb",
    "agent_id": "11111111-1111-4111-8111-111111111111",
    "registration": {"registered_at": "2026-09-23T00:00:00Z", "method": "invite"},
    "credential": {
        "type": "ed25519",
        "agent_key_id": "22222222-2222-4222-8222-222222222222",
        "private_key_ref": "keys/agent-ed25519.key",
    },
    "operator_config_id": "33333333-3333-4333-8333-333333333333",
    "config_version": "0.4",
}

PROFILE = {
    "schema_version": "1",
    "display_name": "Example Agent",
    "model": "model-a",
    "runtime_type": "local-client",
    "locale": "en-US",
    "scheduler_mode": "human_triggered",
    "runtime_capabilities": {"web_search": False, "external_tools": False},
}

STATE = {
    "schema_version": "1",
    "last_run_at": None,
    "last_successful_sync_at": None,
    "feed_cursor": None,
    "latest_activity_at": None,
    "rolling_check_timestamps": [],
    "rolling_action_timestamps": [],
    "cached_manifest_meta": {"version": None, "fetched_at": None, "expires_at": None},
    "control_versions": {"policy": None, "protocol": None, "manifest": None},
    "maintenance": {"active": False, "retry_after_until": None},
    "social": {
        "inbox_cursor": None,
        "notice_cursor": None,
        "own_activity_cursor": None,
        "participated_threads_cursor": None,
        "memory": {"updated_at": None, "window_start": None, "summary": "", "active_threads": []},
    },
    "auth_session": {"expires_at": None},
}


class LocalStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / ".mas"
        self.store = LocalStateStore(self.root)

    def provision(self) -> None:
        self.store.provision_identity(copy.deepcopy(IDENTITY))

    def test_valid_roundtrip_restart_and_permissions(self) -> None:
        self.provision()
        self.store.write_profile(copy.deepcopy(PROFILE))
        self.store.write_state(copy.deepcopy(STATE))
        restarted = LocalStateStore(self.root)
        self.assertEqual(restarted.read_identity(), IDENTITY)
        self.assertEqual(restarted.read_profile(), PROFILE)
        self.assertEqual(restarted.read_state(), STATE)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.root / "keys").stat().st_mode), 0o700)
        for filename in ("identity.json", "profile.json", "state.json"):
            self.assertEqual(stat.S_IMODE((self.root / filename).stat().st_mode), 0o600)
        self.assertFalse((self.root / "keys" / "agent-ed25519.key").exists())

    def test_atomic_replace_failure_preserves_original_and_cleans_temp(self) -> None:
        self.provision()
        self.store.write_state(copy.deepcopy(STATE))
        before = (self.root / "state.json").read_bytes()
        changed = copy.deepcopy(STATE)
        changed["feed_cursor"] = "next"
        with patch("client.mas_client.local_state.os.replace", side_effect=OSError("simulated crash")):
            with self.assertRaises(OSError):
                self.store.write_state(changed)
        self.assertEqual((self.root / "state.json").read_bytes(), before)
        self.assertEqual(list(self.root.glob(".state.*.tmp")), [])

    def test_missing_and_corrupt_state_does_not_replace_identity(self) -> None:
        with self.assertRaises(IdentityMissingError):
            self.store.read_identity()
        self.provision()
        with self.assertRaises(StateMissingError):
            self.store.read_state()
        (self.root / "state.json").write_text("{broken", encoding="utf-8")
        with self.assertRaises(StateValidationError):
            self.store.read_state()
        self.assertEqual(self.store.read_identity()["agent_id"], IDENTITY["agent_id"])
        (self.root / "identity.json").write_text("{broken", encoding="utf-8")
        with self.assertRaises(StateValidationError):
            self.store.read_identity()

    def test_identity_cannot_be_silently_overwritten_or_recreated(self) -> None:
        self.provision()
        different = copy.deepcopy(IDENTITY)
        different["agent_id"] = "44444444-4444-4444-8444-444444444444"
        with self.assertRaises(IdentityConflictError):
            self.store.provision_identity(different)
        with self.assertRaises(IdentityConflictError):
            self.store.update_identity(different)
        (self.root / "identity.json").unlink()
        with self.assertRaises(IdentityMissingError):
            self.store.write_state(copy.deepcopy(STATE))
        (self.root / "profile.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(IdentityConflictError):
            self.store.provision_identity(different)

    def test_profile_state_and_key_reference_can_change_without_new_agent(self) -> None:
        self.provision()
        self.store.write_profile(copy.deepcopy(PROFILE))
        self.store.write_state(copy.deepcopy(STATE))
        updated_profile = copy.deepcopy(PROFILE)
        updated_profile["display_name"] = "Renamed Agent"
        updated_profile["model"] = "other-vendor-model"
        updated_profile["runtime_type"] = "other-runtime"
        updated_state = copy.deepcopy(STATE)
        updated_state["feed_cursor"] = "cursor-2"
        updated_identity = copy.deepcopy(IDENTITY)
        updated_identity["credential"]["agent_key_id"] = "55555555-5555-4555-8555-555555555555"
        self.store.write_profile(updated_profile)
        self.store.write_state(updated_state)
        self.store.update_identity(updated_identity)
        self.assertEqual(self.store.read_identity()["agent_id"], IDENTITY["agent_id"])
        self.assertEqual(self.store.read_profile(), updated_profile)
        self.assertEqual(self.store.read_state(), updated_state)

    def test_private_key_material_and_unknown_fields_are_rejected(self) -> None:
        self.provision()
        forbidden = copy.deepcopy(IDENTITY)
        forbidden["credential"]["private_key"] = "-----BEGIN PRIVATE KEY-----"
        with self.assertRaises(StateValidationError):
            self.store.update_identity(forbidden)
        forbidden = copy.deepcopy(IDENTITY)
        forbidden["credential"]["private_key_ref"] = "-----BEGIN PRIVATE KEY-----"
        with self.assertRaises(StateValidationError):
            self.store.update_identity(forbidden)
        forbidden = copy.deepcopy(PROFILE)
        forbidden["provider_token"] = "secret"
        with self.assertRaises(StateValidationError):
            self.store.write_profile(forbidden)
        forbidden = copy.deepcopy(STATE)
        forbidden["auth_session"]["access_token"] = "secret"
        with self.assertRaises(StateValidationError):
            self.store.write_state(forbidden)

    def test_version_origin_and_duplicate_keys_are_rejected(self) -> None:
        invalid = copy.deepcopy(IDENTITY)
        invalid["schema_version"] = "2"
        with self.assertRaises(StateValidationError):
            validate_document("identity", invalid)
        invalid = copy.deepcopy(IDENTITY)
        invalid["society_origin"] = "https://mas.example.org/path"
        with self.assertRaises(StateValidationError):
            validate_document("identity", invalid)
        self.provision()
        raw = json.dumps(STATE).replace('"schema_version": "1"', '"schema_version": "1", "schema_version": "1"')
        (self.root / "state.json").write_text(raw, encoding="utf-8")
        with self.assertRaises(StateValidationError):
            self.store.read_state()

    def test_symlink_document_is_rejected(self) -> None:
        self.provision()
        outside = Path(self.temporary.name) / "outside.json"
        outside.write_text(json.dumps(STATE), encoding="utf-8")
        os.symlink(outside, self.root / "state.json")
        with self.assertRaises(StateValidationError):
            self.store.read_state()
        with self.assertRaises(StateValidationError):
            self.store.write_state(copy.deepcopy(STATE))


if __name__ == "__main__":
    unittest.main()
