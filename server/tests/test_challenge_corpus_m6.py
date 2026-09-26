"""Freeze invariants for the canonical 50-Challenge corpus."""

import hashlib
import json
import unittest
from pathlib import Path

import yaml

from app.challenge_corpus import load_challenge_corpus
from app.content import normalize_source_url


SEED = Path("data/challenges_v1.yaml")
CORPUS = Path("data/challenges_v2.yaml")
DRAFT = Path("data/challenges_m5_draft.yaml")
PROVENANCE = Path("data/challenge_provenance_m5.yaml")
SEED_HASH = "8fc9e4d023b50f9aea1467a34a265bc2d35204e8186b6caf3de408a564542438"
FROZEN_HASH = "b73278f6cfa90492f3c4b81086c4400de6890a0aa94d7a3864aa4746b02d6f41"


class FrozenChallengeCorpusTests(unittest.TestCase):
    def test_frozen_hashes_shape_and_fields(self):
        self.assertEqual(hashlib.sha256(SEED.read_bytes()).hexdigest(), SEED_HASH)
        self.assertEqual(hashlib.sha256(CORPUS.read_bytes()).hexdigest(), FROZEN_HASH)
        corpus = load_challenge_corpus(CORPUS)
        entries = corpus["challenges"]
        self.assertEqual(corpus["corpus_id"], "mas-challenges-v2")
        self.assertEqual(len(entries), 50)
        self.assertEqual(len({e["stimulus_group_id"] for e in entries}), 50)
        self.assertTrue(all(e["title"] and e["prompt"] and e["field"] for e in entries))
        self.assertTrue(all(e["sources"] for e in entries))
        self.assertTrue(all(e["language"] == "en" and e["version"] == 1 for e in entries))
        self.assertTrue(all(e["active"] for e in entries))
        raw = yaml.safe_load(CORPUS.read_text())
        self.assertTrue(all("answer_key" not in e and "scoring" not in e for e in raw["challenges"]))

    def test_seed_unchanged_and_m5_reviewed_wording(self):
        seed = load_challenge_corpus(SEED)["challenges"]
        entries = load_challenge_corpus(CORPUS)["challenges"]
        self.assertEqual(entries[:18], seed)
        draft = yaml.safe_load(DRAFT.read_text())
        self.assertEqual(draft["social_prompting"], "none")
        self.assertEqual(len(draft["challenges"]), 32)
        for number, (reviewed, final) in enumerate(zip(draft["challenges"], entries[18:]), start=1):
            self.assertEqual(reviewed["draft_id"], f"M5-{number:03d}")
            self.assertEqual(final["stimulus_group_id"], f"CH-M5-{number:03d}")
            self.assertEqual(final["title"], reviewed["title"])
            self.assertEqual(final["prompt"], reviewed["challenge_text"])
            self.assertEqual(final["field"], reviewed["domain"])
            self.assertEqual(final["display_summary"], reviewed["title"])
            self.assertEqual(final["challenge_type"], "open")
            self.assertEqual(
                [source["source_url"] for source in final["sources"]],
                [normalize_source_url(url) for url in reviewed["source_refs"]],
            )

    def test_provenance_mapping_complete(self):
        mapping = yaml.safe_load(PROVENANCE.read_text())
        self.assertEqual(mapping["canonical_corpus_sha256"], FROZEN_HASH)
        self.assertEqual(mapping["draft_sha256"], hashlib.sha256(DRAFT.read_bytes()).hexdigest())
        self.assertEqual(mapping["social_prompting"], "none")
        self.assertEqual(len(mapping["m5_additions"]), 32)
        draft = yaml.safe_load(DRAFT.read_text())["challenges"]
        for item, reviewed in zip(mapping["m5_additions"], draft):
            self.assertEqual(item["draft_id"], reviewed["draft_id"])
            self.assertEqual(item["stimulus_group_id"], f"CH-{reviewed['draft_id']}")
            self.assertEqual(item["source_candidate_id"], reviewed["source_candidate_id"])
            self.assertEqual(item["source_refs"], reviewed["source_refs"])
            path, candidate_id = item["source_record"].split("#", 1)
            self.assertEqual(candidate_id, reviewed["source_candidate_id"])
            source_file = Path(path)
            self.assertTrue(source_file.is_file())
            record = json.loads(source_file.read_text()) if path.endswith(".json") else yaml.safe_load(source_file.read_text())
            pool = record["candidates"] if path.endswith(".json") else record["sources"]
            self.assertTrue(any(r.get("candidate_id", r.get("source_id")) == candidate_id for r in pool))


if __name__ == "__main__":
    unittest.main()
