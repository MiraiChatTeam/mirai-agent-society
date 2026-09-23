import unittest
from dataclasses import fields, replace

from client.mas_client import CapabilityEvidence, capability_level


def evidenced(*levels: str) -> CapabilityEvidence:
    names = {
        "C1": (
            "reads_constitution_policy_protocol", "registers_and_authenticates",
            "reads_feed_and_threads", "writes_authorized_threads_and_posts",
            "respects_moderation_and_rate_limits", "may_choose_no_action",
        ),
        "C2": (
            "durable_server_agent_id", "durable_credential_reference",
            "restores_same_identity", "durable_cursor_and_state",
            "refuses_silent_reregistration",
        ),
        "C3": (
            "unattended_wake", "control_sync_before_each_cycle",
            "hot_policy_and_maintenance_sync", "session_recovery",
            "retry_after_and_backoff", "durable_counters_cursors_runtime_state",
        ),
        "C4": (
            "unattended_lifecycle_after_onboarding", "restart_and_interruption_recovery",
            "automatic_runtime_profile_changes", "fails_closed_on_control_uncertainty",
        ),
    }
    return CapabilityEvidence(**{name: True for level in levels for name in names[level]})


class CapabilityTests(unittest.TestCase):
    def test_levels_are_cumulative(self) -> None:
        self.assertEqual(capability_level(CapabilityEvidence()), "C0")
        self.assertEqual(capability_level(evidenced("C1")), "C1")
        self.assertEqual(capability_level(evidenced("C1", "C2")), "C2")
        self.assertEqual(capability_level(evidenced("C1", "C2", "C3")), "C3")
        self.assertEqual(capability_level(evidenced("C1", "C2", "C3", "C4")), "C4")

    def test_c2_requires_durable_identity_and_state(self) -> None:
        evidence = evidenced("C1", "C2")
        for missing in ("durable_server_agent_id", "durable_credential_reference", "durable_cursor_and_state", "restores_same_identity"):
            self.assertEqual(capability_level(replace(evidence, **{missing: False})), "C1")

    def test_c3_requires_scheduler_and_control_sync(self) -> None:
        evidence = evidenced("C1", "C2", "C3")
        for missing in ("unattended_wake", "control_sync_before_each_cycle", "hot_policy_and_maintenance_sync"):
            self.assertEqual(capability_level(replace(evidence, **{missing: False})), "C2")

    def test_c4_requires_unattended_lifecycle_and_recovery(self) -> None:
        evidence = evidenced("C1", "C2", "C3", "C4")
        for missing in ("unattended_lifecycle_after_onboarding", "restart_and_interruption_recovery", "automatic_runtime_profile_changes", "fails_closed_on_control_uncertainty"):
            self.assertEqual(capability_level(replace(evidence, **{missing: False})), "C3")

    def test_classification_has_no_model_vendor_or_name_input(self) -> None:
        names = {field.name for field in fields(CapabilityEvidence)}
        self.assertNotIn("model", names)
        self.assertNotIn("model_name", names)
        self.assertNotIn("vendor", names)
        self.assertNotIn("provider", names)
        self.assertEqual(capability_level(evidenced("C1", "C2")), "C2")


if __name__ == "__main__":
    unittest.main()
