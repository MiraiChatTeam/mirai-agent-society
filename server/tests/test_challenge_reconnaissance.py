"""Validate the internal M4 reconnaissance pool without publishing candidates."""

import hashlib
import json
import re
import unittest
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path.cwd()
POOL = ROOT / "data/challenge_reconnaissance_m4.json"
CORPUS = ROOT / "data/challenges_v1.yaml"
SEED_SHA256 = "8fc9e4d023b50f9aea1467a34a265bc2d35204e8186b6caf3de408a564542438"
SOCIAL_KEYS = {
    "independently_answerable", "multiple_plausible_approaches",
    "partial_contribution_possible", "correction_opportunity",
    "evidence_disagreement_possible", "specialization_opportunity",
    "cumulative_progress_possible", "revisitation_possible",
    "one_shot_answer_risk",
}
TASK_PROPERTIES = {
    "search_required", "multiple_sources_required", "executable_artifact",
    "information_retrieval", "incomplete_or_noisy_evidence", "prediction",
    "historical_cutoff",
}
EPISTEMIC_TYPES = {
    "deduction", "diagnosis", "evidence_synthesis", "design", "prediction",
    "explanation", "falsification", "information_retrieval",
}
CANDIDATE_KEYS = {
    "candidate_id", "domain", "situation_summary",
    "why_it_may_be_useful_for_mas", "source_urls", "source_family",
    "social_affordances", "task_properties", "epistemic_type",
    "answerability_notes", "limitations_or_risks",
    "provenance_confidence", "review_status", "triage_categories",
    "social_structure_rationale", "evidence_evolution",
    "operational_practicality", "triage_notes", "epistemic_environment",
    "ground_truth_status", "strong_agent_closure_risk", "epistemic_framing_note",
}
TRIAGE_CATEGORIES = {
    "structurally_social", "potentially_social", "low_affordance_comparison",
    "benchmark_like", "operationally_awkward",
}
PRIMARY_TRIAGE = {
    "structurally_social", "potentially_social", "low_affordance_comparison",
}
PRACTICALITY_VALUES = {
    "public_access": {"public_page_or_metadata", "mixed_or_restricted"},
    "account_required": {"no_for_linked_page", "possible", "yes"},
    "machine_access_feasibility": {
        "likely_for_metadata", "conditional", "poor_or_unverified",
    },
    "rights_reuse_clarity": {"record_level_review_needed", "mixed_or_restricted"},
    "data_tooling_burden": {"low", "moderate", "high"},
    "privacy_or_high_stakes_risk": {"low", "moderate", "high"},
}
EPISTEMIC_ENVIRONMENTS = {"closed", "constrained_open", "evolving"}
GROUND_TRUTH_STATUSES = {
    "available", "partial", "unavailable", "not_applicable", "future_or_evolving",
}
CLOSURE_RISKS = {"low", "medium", "high"}


class ChallengeReconnaissanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pool = json.loads(POOL.read_text(encoding="utf-8"))
        cls.candidates = cls.pool["candidates"]

    def test_internal_pool_shape_and_frozen_seed(self):
        self.assertEqual(self.pool["schema_version"], 3)
        self.assertEqual(self.pool["phase"], "source_reconnaissance_only")
        self.assertEqual(self.pool["baseline_social_prompting"], "none")
        self.assertEqual(hashlib.sha256(CORPUS.read_bytes()).hexdigest(), SEED_SHA256)
        self.assertGreaterEqual(len(self.candidates), 50)
        self.assertLessEqual(len(self.candidates), 70)
        ids = [candidate["candidate_id"] for candidate in self.candidates]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(re.fullmatch(r"M4-[A-Z]+-[0-9]{2}", item) for item in ids))
        self.assertGreaterEqual(len({item["domain"] for item in self.candidates}), 8)
        self.assertGreaterEqual(len({item["source_family"] for item in self.candidates}), 25)

    def test_every_candidate_is_sourced_annotated_and_unselected(self):
        for item in self.candidates:
            with self.subTest(candidate=item["candidate_id"]):
                self.assertEqual(set(item), CANDIDATE_KEYS)
                self.assertEqual(item["review_status"], "unselected")
                self.assertIn(item["provenance_confidence"], {
                    "portal_verified_record_pending", "case_verified_task_not_adapted",
                })
                for key in (
                    "domain", "situation_summary", "why_it_may_be_useful_for_mas",
                    "source_family", "answerability_notes", "limitations_or_risks",
                ):
                    self.assertTrue(item[key].strip())
                self.assertTrue(re.fullmatch(r"[a-z][a-z0-9_]*", item["source_family"]))
                self.assertTrue(item["source_urls"])
                self.assertEqual(len(item["source_urls"]), len(set(item["source_urls"])))
                for url in item["source_urls"]:
                    parsed = urlsplit(url)
                    self.assertEqual(parsed.scheme, "https")
                    self.assertIsNotNone(parsed.hostname)
                    self.assertNotIn(parsed.hostname, {"localhost", "127.0.0.1"})
                    self.assertIsNone(parsed.username)
                    self.assertIsNone(parsed.password)
                social = item["social_affordances"]
                self.assertEqual(set(social), SOCIAL_KEYS)
                for key in SOCIAL_KEYS - {"one_shot_answer_risk"}:
                    self.assertIn(social[key], {"plausible", "limited", "uncertain"})
                self.assertIn(social["one_shot_answer_risk"], {
                    "limited", "possible", "substantial",
                })
                categories = item["triage_categories"]
                self.assertTrue(categories)
                self.assertEqual(len(categories), len(set(categories)))
                self.assertLessEqual(set(categories), TRIAGE_CATEGORIES)
                self.assertEqual(len(set(categories) & PRIMARY_TRIAGE), 1)
                self.assertTrue(item["social_structure_rationale"].strip())
                self.assertTrue(item["triage_notes"].strip())
                self.assertTrue(item["epistemic_framing_note"].strip())
                self.assertIn(item["epistemic_environment"], EPISTEMIC_ENVIRONMENTS)
                self.assertIn(item["ground_truth_status"], GROUND_TRUTH_STATUSES)
                self.assertIn(item["strong_agent_closure_risk"], CLOSURE_RISKS)
                self.assertIs(type(item["evidence_evolution"]), bool)
                if item["evidence_evolution"]:
                    self.assertEqual(item["epistemic_environment"], "evolving")
                practicality = item["operational_practicality"]
                self.assertEqual(set(practicality), set(PRACTICALITY_VALUES))
                for key, valid_values in PRACTICALITY_VALUES.items():
                    self.assertIn(practicality[key], valid_values)
                if "structurally_social" in categories:
                    for key in (
                        "partial_contribution_possible",
                        "cumulative_progress_possible",
                    ):
                        self.assertEqual(social[key], "plausible")
                if "low_affordance_comparison" in categories:
                    self.assertNotEqual(social["partial_contribution_possible"], "plausible")
                    self.assertNotEqual(social["cumulative_progress_possible"], "plausible")
                self.assertTrue(item["task_properties"])
                self.assertLessEqual(set(item["task_properties"]), TASK_PROPERTIES)
                self.assertEqual(len(item["task_properties"]), len(set(item["task_properties"])))
                self.assertTrue(item["epistemic_type"])
                self.assertLessEqual(set(item["epistemic_type"]), EPISTEMIC_TYPES)
                self.assertEqual(len(item["epistemic_type"]), len(set(item["epistemic_type"])))
                self.assertNotIn("prompt", item)
                self.assertNotIn("challenge_text", item)
                self.assertIsNone(re.search(
                    r"\b(?:ask|consult|mention|compare)\s+(?:another|other|fellow)\s+agent\b"
                    r"|\buse (?:the )?commons\b|\breach consensus\b",
                    item["situation_summary"], re.IGNORECASE,
                ))

    def test_conservative_triage_and_practicality_are_internal(self):
        categories = Counter(
            category for item in self.candidates
            for category in item["triage_categories"]
        )
        self.assertEqual(categories, {
            "structurally_social": 11,
            "potentially_social": 39,
            "low_affordance_comparison": 8,
            "benchmark_like": 21,
            "operationally_awkward": 26,
        })
        self.assertEqual(sum(item["evidence_evolution"] for item in self.candidates), 17)
        self.assertEqual(sum(
            item["social_affordances"]["partial_contribution_possible"] == "plausible"
            for item in self.candidates
        ), 11)
        self.assertEqual(sum(
            item["social_affordances"]["correction_opportunity"] == "plausible"
            for item in self.candidates
        ), 25)
        self.assertTrue(all("social_prompting" not in item for item in self.candidates))

    def test_epistemic_environment_is_orthogonal_to_social_triage(self):
        by_id = {item["candidate_id"]: item for item in self.candidates}
        self.assertEqual(Counter(
            item["epistemic_environment"] for item in self.candidates
        ), {"closed": 15, "constrained_open": 26, "evolving": 17})
        self.assertEqual(Counter(
            item["ground_truth_status"] for item in self.candidates
        ), {
            "available": 15, "partial": 34, "future_or_evolving": 6,
            "unavailable": 1, "not_applicable": 2,
        })
        self.assertEqual(Counter(
            item["strong_agent_closure_risk"] for item in self.candidates
        ), {"high": 23, "medium": 26, "low": 9})
        cross = Counter(
            (item["epistemic_environment"], category)
            for item in self.candidates for category in item["triage_categories"]
        )
        self.assertEqual(cross[("closed", "benchmark_like")], 12)
        self.assertEqual(cross[("constrained_open", "structurally_social")], 1)
        self.assertEqual(cross[("evolving", "structurally_social")], 10)
        self.assertEqual(cross[("evolving", "operationally_awkward")], 11)

        # These demonstrate that known truth, evolution, and social affordance
        # are not mechanically equated with one another.
        self.assertEqual(by_id["M4-MATH-05"]["epistemic_environment"], "constrained_open")
        self.assertIn("structurally_social", by_id["M4-MATH-05"]["triage_categories"])
        self.assertEqual(by_id["M4-ENG-02"]["ground_truth_status"], "partial")
        self.assertEqual(by_id["M4-PHYS-05"]["strong_agent_closure_risk"], "high")
        self.assertIn("benchmark_like", by_id["M4-PHYS-05"]["triage_categories"])
        self.assertEqual(by_id["M4-DATA-03"]["epistemic_environment"], "closed")
        self.assertIn("prediction", by_id["M4-DATA-03"]["task_properties"])
        self.assertEqual(by_id["M4-DATA-04"]["ground_truth_status"], "unavailable")
        self.assertEqual(by_id["M4-LANG-02"]["epistemic_environment"], "constrained_open")
        self.assertEqual(by_id["M4-LANG-02"]["ground_truth_status"], "not_applicable")
        self.assertIn("human review", by_id["M4-LANG-02"]["answerability_notes"])

    def test_natural_comparison_cases_and_ecology_are_present(self):
        one_shot = [
            item for item in self.candidates
            if item["social_affordances"]["one_shot_answer_risk"] == "substantial"
        ]
        self.assertGreaterEqual(len(one_shot), 5)
        counts = Counter(
            property_name for item in self.candidates
            for property_name in item["task_properties"]
        )
        for property_name in TASK_PROPERTIES:
            self.assertGreaterEqual(counts[property_name], 5)


if __name__ == "__main__":
    unittest.main()
