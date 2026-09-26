"""Reproduce the M6 corpus and M5 provenance mapping from frozen inputs."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

import yaml


ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "data/challenges_v1.yaml"
DRAFT = ROOT / "data/challenges_m5_draft.yaml"
CORPUS = ROOT / "data/challenges_v2.yaml"
PROVENANCE = ROOT / "data/challenge_provenance_m5.yaml"
SEED_SHA256 = "8fc9e4d023b50f9aea1467a34a265bc2d35204e8186b6caf3de408a564542438"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build() -> tuple[bytes, bytes]:
    seed_bytes = SEED.read_bytes()
    if sha256(seed_bytes) != SEED_SHA256:
        raise ValueError("the original 18-Challenge seed hash differs; stop")
    seed = yaml.safe_load(seed_bytes)
    draft_bytes = DRAFT.read_bytes()
    draft = yaml.safe_load(draft_bytes)
    if len(seed["challenges"]) != 18 or len(draft["challenges"]) != 32:
        raise ValueError("expected exactly 18 seed and 32 reviewed M5 Challenges")
    if draft["social_prompting"] != "none" or draft["seed_corpus_sha256"] != SEED_SHA256:
        raise ValueError("the M5 draft does not match the unprompted frozen seed")

    pools = {
        "data/challenge_reconnaissance_m4.json": json.loads(
            (ROOT / "data/challenge_reconnaissance_m4.json").read_text()
        )["candidates"],
        "data/challenge_reconnaissance_m41.json": json.loads(
            (ROOT / "data/challenge_reconnaissance_m41.json").read_text()
        )["candidates"],
        "data/challenge_sources_m5_chemistry.yaml": yaml.safe_load(
            (ROOT / "data/challenge_sources_m5_chemistry.yaml").read_text()
        )["sources"],
    }
    source_records = {}
    for path, records in pools.items():
        for record in records:
            key = record.get("candidate_id", record.get("source_id"))
            if key in source_records:
                raise ValueError(f"duplicate source candidate: {key}")
            source_records[key] = (path, record)

    additions = []
    mappings = []
    identifiers = {item["stimulus_group_id"] for item in seed["challenges"]}
    for number, item in enumerate(draft["challenges"], start=1):
        draft_id = f"M5-{number:03d}"
        if item["draft_id"] != draft_id:
            raise ValueError(f"nonsequential M5 draft ID: {item['draft_id']}")
        final_id = f"CH-{draft_id}"
        if final_id in identifiers:
            raise ValueError(f"duplicate Challenge ID: {final_id}")
        identifiers.add(final_id)
        candidate_id = item["source_candidate_id"]
        if candidate_id not in source_records:
            raise ValueError(f"missing reconnaissance record: {candidate_id}")
        source_path, record = source_records[candidate_id]
        references = item["source_refs"]
        if not references or len(set(references)) != len(references):
            raise ValueError(f"missing or duplicate sources: {draft_id}")
        sources = []
        for position, url in enumerate(references, start=1):
            host = urlsplit(url).hostname
            if not host or urlsplit(url).scheme != "https":
                raise ValueError(f"invalid source URL: {draft_id}")
            sources.append({
                "source_kind": "literature_anchored",
                "source_name": f"{host} — {draft_id} reference {position}",
                "source_url": url,
                "source_role": "background_anchor",
            })
        additions.append({
            "stimulus_group_id": final_id,
            "challenge_type": "open",
            "field": item["domain"],
            "title": item["title"],
            "display_summary": item["title"],
            "prompt": item["challenge_text"],
            "sources": sources,
        })
        mapping = {
            "draft_id": draft_id,
            "stimulus_group_id": final_id,
            "source_candidate_id": candidate_id,
            "source_record": f"{source_path}#{candidate_id}",
            "source_refs": references,
        }
        if record.get("source_family"):
            mapping["source_family"] = record["source_family"]
        mappings.append(mapping)

    seed_text = seed_bytes.decode("utf-8")
    old_header = "corpus_id: mas-challenges-v1\n"
    if not seed_text.startswith(old_header):
        raise ValueError("unexpected original seed header")
    header_and_seed = "corpus_id: mas-challenges-v2\n" + seed_text[len(old_header):]
    additions_yaml = "".join(
        "  " + line
        for line in yaml.safe_dump(
            {"challenges": additions}, allow_unicode=True, sort_keys=False, width=100
        ).split("\n", 1)[1].splitlines(keepends=True)
    )
    corpus_bytes = (header_and_seed.rstrip("\n") + "\n\n" + additions_yaml).encode("utf-8")
    provenance = {
        "schema_version": 1,
        "corpus_id": "mas-challenges-v2",
        "canonical_corpus_sha256": sha256(corpus_bytes),
        "draft_set_id": draft["draft_set_id"],
        "draft_sha256": sha256(draft_bytes),
        "social_prompting": "none",
        "provenance_note": (
            "These references ground original MAS questions; they are not answer keys, "
            "task origins, required reading, or reuse permissions."
        ),
        "m5_additions": mappings,
    }
    provenance_bytes = yaml.safe_dump(
        provenance, allow_unicode=True, sort_keys=False, width=100
    ).encode("utf-8")
    return corpus_bytes, provenance_bytes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="write canonical derived files")
    args = parser.parse_args()
    corpus_bytes, provenance_bytes = build()
    if args.write:
        CORPUS.write_bytes(corpus_bytes)
        PROVENANCE.write_bytes(provenance_bytes)
    elif CORPUS.read_bytes() != corpus_bytes or PROVENANCE.read_bytes() != provenance_bytes:
        raise SystemExit("frozen corpus/provenance differs from reviewed inputs")
    print(f"50 Challenges; canonical SHA-256 {sha256(corpus_bytes)}")


if __name__ == "__main__":
    main()
