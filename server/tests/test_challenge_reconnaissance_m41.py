"""Integrity checks for the internal M4.1 gap-directed source sidecar."""

import json
import unittest
from pathlib import Path


ROOT = Path.cwd()
M4 = ROOT / "data" / "challenge_reconnaissance_m4.json"
M41 = ROOT / "data" / "challenge_reconnaissance_m41.json"


class M41ReconnaissanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original = json.loads(M4.read_text(encoding="utf-8"))
        cls.expansion = json.loads(M41.read_text(encoding="utf-8"))
        cls.candidates = cls.expansion["candidates"]

    def test_separate_unselected_sidecar(self):
        self.assertEqual(len(self.original["candidates"]), 58)
        self.assertEqual(self.expansion["parent_pool"], M4.name)
        self.assertEqual(self.expansion["phase"], "gap_directed_source_reconnaissance_only")
        self.assertEqual(self.expansion["social_prompting"], "none")
        self.assertLessEqual(len(self.candidates), 20)
        old_ids = {item["candidate_id"] for item in self.original["candidates"]}
        new_ids = [item["candidate_id"] for item in self.candidates]
        self.assertEqual(len(new_ids), len(set(new_ids)))
        self.assertFalse(old_ids.intersection(new_ids))

    def test_each_candidate_has_concrete_marginal_value_and_m4_fields(self):
        required = {
            "candidate_id", "domain", "situation_summary", "why_it_may_be_useful_for_mas",
            "source_urls", "source_family", "social_affordances", "task_properties",
            "epistemic_type", "answerability_notes", "limitations_or_risks",
            "provenance_confidence", "review_status", "triage_categories",
            "social_structure_rationale", "second_agent_marginal_value", "evidence_evolution",
            "operational_practicality", "triage_notes", "epistemic_environment",
            "ground_truth_status", "strong_agent_closure_risk", "epistemic_framing_note",
        }
        affordances = set(self.original["candidates"][0]["social_affordances"])
        practicality = set(self.original["candidates"][0]["operational_practicality"])
        for item in self.candidates:
            with self.subTest(candidate=item["candidate_id"]):
                self.assertEqual(set(item), required)
                self.assertTrue(item["candidate_id"].startswith("M41-"))
                self.assertEqual(item["review_status"], "unselected")
                self.assertEqual(item["provenance_confidence"], "portal_verified_record_pending")
                self.assertEqual(set(item["social_affordances"]), affordances)
                self.assertEqual(set(item["operational_practicality"]), practicality)
                self.assertTrue(item["second_agent_marginal_value"].strip())
                self.assertGreaterEqual(len(item["source_urls"]), 2)
                self.assertTrue(all(url.startswith("https://") for url in item["source_urls"]))
                self.assertIn("structurally_social", item["triage_categories"])
                self.assertIn(item["epistemic_environment"], {"closed", "constrained_open", "evolving"})
                self.assertIn(item["strong_agent_closure_risk"], {"low", "medium", "high"})

    def test_reported_counts(self):
        self.assertEqual(len(self.candidates), 4)
        self.assertEqual(sum("structurally_social" in c["triage_categories"] for c in self.candidates), 4)
        self.assertEqual(sum(c["epistemic_environment"] == "constrained_open" for c in self.candidates), 0)
        self.assertEqual(sum(c["epistemic_environment"] == "evolving" for c in self.candidates), 4)
        self.assertEqual(sum(c["strong_agent_closure_risk"] == "low" for c in self.candidates), 2)
        self.assertEqual(sum("operationally_awkward" in c["triage_categories"] for c in self.candidates), 0)


if __name__ == "__main__":
    unittest.main()
