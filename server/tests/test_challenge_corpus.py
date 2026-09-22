import unittest
from collections import Counter
from pathlib import Path

from app.challenge_corpus import load_challenge_corpus


CORPUS = Path("data/challenges_v1.yaml")


class ChallengeCorpusTests(unittest.TestCase):
    def test_fixed_v1_shape_and_counts(self):
        corpus = load_challenge_corpus(CORPUS)
        challenges = corpus["challenges"]
        self.assertEqual(corpus["corpus_id"], "mas-challenges-v1")
        self.assertEqual(len(challenges), 18)
        self.assertEqual(
            Counter(item["challenge_type"] for item in challenges),
            {"verifiable": 6, "open": 6, "debatable": 6},
        )
        self.assertEqual({item["language"] for item in challenges}, {"en"})
        self.assertEqual({item["version"] for item in challenges}, {1})
        self.assertEqual({item["active"] for item in challenges}, {True})
        self.assertEqual(len({item["stimulus_group_id"] for item in challenges}), 18)
        self.assertTrue(
            all(1 <= len(item["display_summary"].split()) <= 20 for item in challenges)
        )

    def test_generated_and_literature_provenance(self):
        challenges = load_challenge_corpus(CORPUS)["challenges"]
        generated = [
            item for item in challenges if item["challenge_type"] == "verifiable"
        ]
        anchored = [
            item for item in challenges if item["challenge_type"] != "verifiable"
        ]
        self.assertTrue(
            all(item["sources"][0]["source_kind"] == "generated" for item in generated)
        )
        self.assertTrue(
            all(
                item["sources"][0]["source_kind"] == "literature_anchored"
                and item["sources"][0]["source_url"].startswith("https://")
                for item in anchored
            )
        )
        biology_open = next(
            item for item in challenges if item["stimulus_group_id"] == "CH-BIO-001"
        )
        self.assertEqual(
            biology_open["sources"][0]["source_url"],
            "https://pubmed.ncbi.nlm.nih.gov/40877537",
        )


if __name__ == "__main__":
    unittest.main()
