"""Boundary checks for the refined, still unpublished local v1 layout."""

import copy
import tempfile
import unittest
from pathlib import Path

from client.mas_client.local_state import LocalStateStore, StateValidationError, validate_document
from client.tests.test_local_state import IDENTITY, PROFILE, STATE


class StateLayoutRefinementTests(unittest.TestCase):
    def test_runtime_capabilities_are_not_operator_authorization(self) -> None:
        profile = copy.deepcopy(PROFILE)
        profile["runtime_capabilities"] = {"web_search": True, "external_tools": False}
        validate_document("profile", profile)

        old_authorization_name = copy.deepcopy(profile)
        old_authorization_name["tools"] = old_authorization_name.pop("runtime_capabilities")
        with self.assertRaises(StateValidationError):
            validate_document("profile", old_authorization_name)

        with_authorization = copy.deepcopy(profile)
        with_authorization["operator_authorization"] = {"web_search": False}
        with self.assertRaises(StateValidationError):
            validate_document("profile", with_authorization)

    def test_control_versions_persist_in_state_not_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / ".mas"
            store = LocalStateStore(root)
            store.provision_identity(copy.deepcopy(IDENTITY))
            store.write_profile(copy.deepcopy(PROFILE))
            state = copy.deepcopy(STATE)
            state["control_versions"] = {
                "policy": "0.1", "protocol": "0.1", "manifest": "2026-09-23"
            }
            state["cached_manifest_meta"] = {
                "version": "2026-09-23",
                "fetched_at": "2026-09-23T01:00:00Z",
                "expires_at": "2026-09-23T02:00:00Z",
            }
            store.write_state(state)

            restarted = LocalStateStore(root)
            self.assertEqual(restarted.read_state(), state)
            self.assertNotIn("control_versions", restarted.read_profile())
            self.assertEqual(restarted.read_identity(), IDENTITY)

    def test_earlier_local_draft_fixture_requires_explicit_field_mapping(self) -> None:
        legacy_profile = copy.deepcopy(PROFILE)
        legacy_profile["tools"] = legacy_profile.pop("runtime_capabilities")
        legacy_profile["last_known_versions"] = {
            "policy": "0.1", "protocol": "0.1", "manifest": None
        }
        legacy_state = copy.deepcopy(STATE)
        legacy_state["cached_manifest"] = legacy_state.pop("cached_manifest_meta")
        legacy_state.pop("control_versions")

        with self.assertRaises(StateValidationError):
            validate_document("profile", legacy_profile)
        with self.assertRaises(StateValidationError):
            validate_document("state", legacy_state)

        mapped_profile = copy.deepcopy(legacy_profile)
        mapped_profile["runtime_capabilities"] = mapped_profile.pop("tools")
        versions = mapped_profile.pop("last_known_versions")
        mapped_state = copy.deepcopy(legacy_state)
        mapped_state["cached_manifest_meta"] = mapped_state.pop("cached_manifest")
        mapped_state["control_versions"] = versions

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / ".mas"
            store = LocalStateStore(root)
            store.provision_identity(copy.deepcopy(IDENTITY))
            store.write_profile(mapped_profile)
            store.write_state(mapped_state)
            restarted = LocalStateStore(root)
            self.assertEqual(restarted.read_profile(), mapped_profile)
            self.assertEqual(restarted.read_state()["control_versions"], versions)
            self.assertEqual(restarted.read_identity(), IDENTITY)


if __name__ == "__main__":
    unittest.main()
