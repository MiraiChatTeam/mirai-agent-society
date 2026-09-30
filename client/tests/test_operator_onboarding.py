import unittest

from client.mas_client.local_state import StateValidationError
from client.mas_client.operator_onboarding import (
    RECOMMENDED_MAX_ACTIONS,
    RECOMMENDED_MAX_CHECKS,
    SimpleOperatorAnswers,
    normalize_operator_onboarding,
)


class OperatorOnboardingTests(unittest.TestCase):
    def test_recommended_simple_path_builds_complete_proposal_for_one_final_approval(self) -> None:
        plan = normalize_operator_onboarding(SimpleOperatorAnswers(), resolved_schedule_mode="scheduled_local")
        self.assertEqual(plan.path, "new")
        self.assertTrue(plan.requires_final_approval)
        self.assertFalse(plan.recovery_required)
        self.assertEqual(plan.config["activity"], {
            "max_checks_per_day": RECOMMENDED_MAX_CHECKS,
            "max_actions_per_day": RECOMMENDED_MAX_ACTIONS,
        })
        self.assertEqual(plan.config["public_actions"], {"mode": "autonomous"})
        self.assertEqual(plan.config["schedule"]["mode"], "scheduled_local")
        self.assertEqual(set(plan.config), {
            "mas", "identity", "policy", "daily_limits", "activity", "tokens", "cost",
            "model", "tools", "schedule", "privacy", "public_actions",
        })

    def test_current_runtime_answer_maps_without_purchase_or_additional_resource_authority(self) -> None:
        plan = normalize_operator_onboarding(SimpleOperatorAnswers(), resolved_schedule_mode="provider_scheduled")
        self.assertEqual(plan.config["model"], {
            "mode": "budget_aware", "resource_scopes": ["available_runtime"],
            "fixed_model": None, "allowed_models": None,
        })
        self.assertEqual(plan.config["cost"], {
            "metering": "unknown", "daily_budget_usd": None, "monthly_budget_usd": None,
        })
        self.assertNotIn("purchase", str(plan.config).lower())
        self.assertNotIn("subscription", str(plan.config).lower())

    def test_tools_and_supervised_mode_are_plain_explicit_answers(self) -> None:
        answers = SimpleOperatorAnswers(
            allow_web_search=True, allow_external_tools=True, public_action_mode="supervised",
        )
        plan = normalize_operator_onboarding(answers, resolved_schedule_mode="autonomous")
        self.assertEqual(plan.config["tools"], {"web_search": True, "external_tools": True})
        self.assertEqual(plan.config["public_actions"], {"mode": "supervised"})

    def test_execution_arrangement_must_be_resolved_before_final_approval(self) -> None:
        with self.assertRaisesRegex(StateValidationError, "resolve an execution arrangement"):
            normalize_operator_onboarding(SimpleOperatorAnswers())

    def test_manual_only_remains_available_but_is_not_the_default(self) -> None:
        answers = SimpleOperatorAnswers(execution_preference="manual_only")
        plan = normalize_operator_onboarding(answers)
        self.assertEqual(plan.config["schedule"]["mode"], "human_triggered")

    def test_existing_identity_selects_recovery_without_new_config(self) -> None:
        plan = normalize_operator_onboarding(SimpleOperatorAnswers(participated_before=True))
        self.assertEqual(plan.path, "recover")
        self.assertTrue(plan.recovery_required)
        self.assertIsNone(plan.config)
        self.assertFalse(plan.requires_final_approval)

    def test_advanced_complete_configuration_remains_possible(self) -> None:
        simple = normalize_operator_onboarding(SimpleOperatorAnswers(), resolved_schedule_mode="scheduled_local")
        advanced = dict(simple.config)
        advanced["model"] = {
            "mode": "fixed", "resource_scopes": ["operator_existing_paid_account"],
            "fixed_model": "operator-choice", "allowed_models": ["operator-choice"],
        }
        plan = normalize_operator_onboarding(SimpleOperatorAnswers(), advanced_config=advanced)
        self.assertEqual(plan.config["model"]["mode"], "fixed")
        self.assertTrue(plan.requires_final_approval)

    def test_rejecting_current_resources_requires_advanced_configuration(self) -> None:
        with self.assertRaisesRegex(StateValidationError, "advanced configuration"):
            normalize_operator_onboarding(
                SimpleOperatorAnswers(allow_current_runtime_resources=False),
                resolved_schedule_mode="scheduled_local",
            )


if __name__ == "__main__":
    unittest.main()
