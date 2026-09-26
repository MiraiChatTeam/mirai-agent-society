"""Validate the internal v1 sourcing sidecar without exposing it to Agents."""

import hashlib
import re
import unittest
import uuid
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from app.challenge_corpus import load_challenge_corpus
from app.content import normalize_source_url


CORPUS = Path("data/challenges_v1.yaml")
ANNOTATIONS = Path("data/challenge_sourcing_v1.yaml")
EPISTEMIC = {
    "deduction", "diagnosis", "evidence_synthesis", "design", "prediction",
    "explanation", "falsification", "information_retrieval",
}
VERIFICATION = {
    "exact_answer", "formal_proof", "unit_tests", "known_historical_outcome",
    "reference_evidence", "expert_rubric", "consistency_check",
}
DIRECT_SOCIAL = re.compile(
    r"\b(?:ask|consult|mention|compare)\b.{0,70}\b(?:another|other|fellow)\s+agent\b"
    r"|\bagent commons\b|\bcompare agent opinions\b",
    re.IGNORECASE,
)


def labels(items, path):
    counts = Counter()
    for item in items:
        value = item
        for part in path:
            value = value[part]
        counts.update(value if isinstance(value, list) else [value])
    return counts


class ChallengeSourcingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = load_challenge_corpus(CORPUS)
        cls.document = yaml.safe_load(ANNOTATIONS.read_text(encoding="utf-8"))
        cls.items = cls.document["annotations"]

    def test_exact_coverage_order_and_provenance(self):
        self.assertEqual(set(self.document), {"schema_version", "corpus_id", "corpus_sha256", "annotations"})
        self.assertEqual(self.document["schema_version"], 2)
        self.assertEqual(
            self.document["corpus_sha256"], hashlib.sha256(CORPUS.read_bytes()).hexdigest()
        )
        self.assertEqual(self.document["corpus_id"], self.corpus["corpus_id"])
        self.assertEqual(len(self.items), 18)
        self.assertEqual(
            [item["stimulus_group_id"] for item in self.items],
            [item["stimulus_group_id"] for item in self.corpus["challenges"]],
        )
        ids = [uuid.UUID(item["challenge_id"]) for item in self.items]
        self.assertEqual(len(set(ids)), 18)
        for annotation, challenge in zip(self.items, self.corpus["challenges"]):
            self.assertEqual(set(annotation), {
                "challenge_id", "stimulus_group_id", "provenance",
                "classification", "evaluation", "experimental", "curation",
            })
            source = challenge["sources"][0]
            provenance = annotation["provenance"]
            self.assertEqual(set(provenance), {
                "source_type", "source_family", "source_reference",
                "adaptation_level", "exposure_risk",
            })
            self.assertIn(provenance["adaptation_level"], {
                "none", "parameterized", "transformed", "reconstructed", "MAS_original",
            })
            self.assertIn(provenance["exposure_risk"], {"low", "medium", "high"})
            if source["source_kind"] == "generated":
                self.assertEqual(source["source_role"], "task_design")
                self.assertEqual(provenance["source_type"], "MAS_generated")
                self.assertEqual(provenance["source_family"], "MAS_original")
                self.assertEqual(provenance["source_reference"], source["source_name"])
                self.assertEqual(provenance["adaptation_level"], "MAS_original")
            else:
                self.assertEqual(source["source_role"], "background_anchor")
                self.assertEqual(provenance["source_type"], "literature_anchored")
                expected_family = {
                    "www.claymath.org": "clay_mathematics_institute",
                    "home.cern": "cern",
                    "science.nasa.gov": "nasa_science",
                    "pubmed.ncbi.nlm.nih.gov": "ncbi_pubmed_pmc",
                    "pmc.ncbi.nlm.nih.gov": "ncbi_pubmed_pmc",
                    "plato.stanford.edu": "stanford_encyclopedia_of_philosophy",
                    "www.nationalacademies.org": "national_academies",
                    "arxiv.org": "arxiv",
                }[urlsplit(provenance["source_reference"]).hostname]
                self.assertEqual(provenance["source_family"], expected_family)
                self.assertEqual(normalize_source_url(provenance["source_reference"]), source["source_url"])
                self.assertEqual(provenance["adaptation_level"], "none")

    def test_complete_rubric_and_no_direct_social_prompting(self):
        for annotation, challenge in zip(self.items, self.corpus["challenges"]):
            classification = annotation["classification"]
            self.assertEqual(set(classification), {
                "domains", "epistemic_type", "answerability",
                "task_structure", "information_structure",
            })
            self.assertEqual(classification["domains"], [challenge["field"]])
            self.assertTrue(classification["epistemic_type"])
            self.assertEqual(len(set(classification["epistemic_type"])),
                             len(classification["epistemic_type"]))
            self.assertLessEqual(set(classification["epistemic_type"]), EPISTEMIC)
            self.assertIn(classification["answerability"],
                          {"closed", "bounded", "open", "insufficient"})
            self.assertIn(classification["task_structure"],
                          {"single_stage", "multi_stage"})
            self.assertIn(classification["information_structure"], {
                "self_contained", "search_useful", "search_required",
                "multiple_sources_required",
            })
            self.assertEqual(set(annotation["evaluation"]), {"verification_method"})
            self.assertIn(annotation["evaluation"]["verification_method"], VERIFICATION)
            self.assertEqual(set(annotation["experimental"]), {"social_prompting"})
            self.assertIn(annotation["experimental"]["social_prompting"],
                          {"none", "mild", "explicit"})
            self.assertEqual(set(annotation["curation"]), {"notes"})
            self.assertTrue(annotation["curation"]["notes"].strip())
            self.assertIsNone(DIRECT_SOCIAL.search(challenge["prompt"]))
            self.assertEqual(annotation["experimental"]["social_prompting"], "none")

    def test_seed_distribution_report(self):
        expected = {
            ("classification", "domains"): {
                "astronomy": 3, "biology": 3, "computer_science": 3,
                "logic": 3, "mathematics": 3, "physics": 3,
            },
            ("classification", "epistemic_type"): {
                "deduction": 6, "diagnosis": 3, "evidence_synthesis": 8,
                "design": 8, "prediction": 0, "explanation": 4,
                "falsification": 5, "information_retrieval": 0,
            },
            ("classification", "answerability"): {
                "closed": 6, "bounded": 6, "open": 6, "insufficient": 0,
            },
            ("classification", "task_structure"): {
                "single_stage": 3, "multi_stage": 15,
            },
            ("classification", "information_structure"): {
                "self_contained": 6, "search_useful": 12,
                "search_required": 0, "multiple_sources_required": 0,
            },
            ("evaluation", "verification_method"): {
                "exact_answer": 4, "formal_proof": 2, "unit_tests": 0,
                "known_historical_outcome": 0, "reference_evidence": 4,
                "expert_rubric": 6, "consistency_check": 2,
            },
            ("provenance", "exposure_risk"): {
                "low": 3, "medium": 7, "high": 8,
            },
            ("provenance", "source_family"): {
                "MAS_original": 6, "ncbi_pubmed_pmc": 4,
                "nasa_science": 2, "stanford_encyclopedia_of_philosophy": 2,
                "clay_mathematics_institute": 1, "cern": 1,
                "national_academies": 1, "arxiv": 1,
            },
            ("experimental", "social_prompting"): {
                "none": 18, "mild": 0, "explicit": 0,
            },
        }
        for path, counts in expected.items():
            with self.subTest(path=path):
                actual = labels(self.items, path)
                self.assertLessEqual(set(actual), set(counts))
                self.assertEqual({label: actual[label] for label in counts}, counts)


if __name__ == "__main__":
    unittest.main()
