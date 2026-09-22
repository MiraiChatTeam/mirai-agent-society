"""Validated, idempotent import and publication of local Challenge corpora."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.content import (
    CHALLENGE_FIELDS,
    CHALLENGE_TYPES,
    LANGUAGES,
    create_challenge,
    normalize_source_url,
    publish_challenge,
)
from app.models import Challenge, ChallengeSource, Thread


SOURCE_KINDS = {"generated", "literature_anchored"}
SOURCE_ROLES = {"task_design", "background_anchor"}
REQUIRED_CHALLENGE_KEYS = {
    "stimulus_group_id",
    "challenge_type",
    "field",
    "title",
    "display_summary",
    "prompt",
    "sources",
}
REQUIRED_SOURCE_KEYS = {"source_kind", "source_name", "source_role"}


def _nonempty_string(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    value = value.strip()
    if not 1 <= len(value) <= maximum:
        raise ValueError(f"{field} must contain between 1 and {maximum} characters")
    return value


def load_challenge_corpus(path: Path) -> dict[str, Any]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"unable to read Challenge corpus: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError("Challenge corpus root must be a mapping")
    corpus_id = _nonempty_string(document.get("corpus_id"), "corpus_id", 100)
    language = document.get("language")
    version = document.get("version")
    active = document.get("active")
    entries = document.get("challenges")
    if language not in LANGUAGES:
        raise ValueError("unsupported corpus language")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError("corpus version must be a positive integer")
    if not isinstance(active, bool):
        raise ValueError("corpus active must be a boolean")
    if not isinstance(entries, list) or not entries:
        raise ValueError("corpus challenges must be a non-empty list")

    normalized_entries: list[dict[str, Any]] = []
    identities: set[tuple[str, str, int]] = set()
    for position, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict) or set(entry) != REQUIRED_CHALLENGE_KEYS:
            raise ValueError(f"challenge {position} has unexpected or missing fields")
        challenge_type = entry["challenge_type"]
        field = entry["field"]
        if challenge_type not in CHALLENGE_TYPES:
            raise ValueError(f"challenge {position} has an unsupported type")
        if field not in CHALLENGE_FIELDS:
            raise ValueError(f"challenge {position} has an unsupported field")
        stimulus_group_id = _nonempty_string(
            entry["stimulus_group_id"], "stimulus_group_id", 64
        )
        identity = (stimulus_group_id, language, version)
        if identity in identities:
            raise ValueError(f"duplicate corpus identity: {stimulus_group_id}")
        identities.add(identity)
        raw_sources = entry["sources"]
        if not isinstance(raw_sources, list) or not raw_sources:
            raise ValueError(f"challenge {stimulus_group_id} requires provenance")
        sources: list[dict[str, str | None]] = []
        source_identities: set[tuple[str, str, str]] = set()
        for source in raw_sources:
            if not isinstance(source, dict) or not REQUIRED_SOURCE_KEYS.issubset(source):
                raise ValueError(f"challenge {stimulus_group_id} has invalid provenance")
            if set(source) - (REQUIRED_SOURCE_KEYS | {"source_url"}):
                raise ValueError(f"challenge {stimulus_group_id} has unknown provenance fields")
            source_kind = source["source_kind"]
            source_role = source["source_role"]
            if source_kind not in SOURCE_KINDS or source_role not in SOURCE_ROLES:
                raise ValueError(f"challenge {stimulus_group_id} has invalid provenance enums")
            source_name = _nonempty_string(source["source_name"], "source_name", 300)
            source_url = source.get("source_url")
            if source_url is not None:
                source_url = normalize_source_url(
                    _nonempty_string(source_url, "source_url", 2048)
                )
            if source_kind == "literature_anchored" and source_url is None:
                raise ValueError("literature_anchored provenance requires source_url")
            source_identity = (source_kind, source_name, source_role)
            if source_identity in source_identities:
                raise ValueError(f"challenge {stimulus_group_id} repeats provenance")
            source_identities.add(source_identity)
            sources.append(
                {
                    "source_kind": source_kind,
                    "source_name": source_name,
                    "source_url": source_url,
                    "source_role": source_role,
                }
            )
        display_summary = _nonempty_string(
            entry["display_summary"], "display_summary", 300
        )
        if len(display_summary.split()) > 20:
            raise ValueError("display_summary must contain at most 20 words")
        normalized_entries.append(
            {
                "stimulus_group_id": stimulus_group_id,
                "challenge_type": challenge_type,
                "field": field,
                "title": _nonempty_string(entry["title"], "title", 300),
                "display_summary": display_summary,
                "prompt": _nonempty_string(entry["prompt"], "prompt", 100_000),
                "language": language,
                "version": version,
                "active": active,
                "sources": sources,
            }
        )
    return {"corpus_id": corpus_id, "challenges": normalized_entries}


def import_challenge_corpus(
    db: Session, path: Path, *, publish: bool = False
) -> dict[str, object]:
    corpus = load_challenge_corpus(path)
    imported = existing_count = sources_added = published_count = already_published = 0
    challenge_ids: list[str] = []
    for entry in corpus["challenges"]:
        challenge = db.scalar(
            select(Challenge).where(
                Challenge.stimulus_group_id == entry["stimulus_group_id"],
                Challenge.language == entry["language"],
                Challenge.version == entry["version"],
            )
        )
        if challenge is None:
            challenge = create_challenge(
                db,
                stimulus_group_id=entry["stimulus_group_id"],
                field=entry["field"],
                title=entry["title"],
                prompt=entry["prompt"],
                language=entry["language"],
                version=entry["version"],
                active=entry["active"],
                challenge_type=entry["challenge_type"],
                display_summary=entry["display_summary"],
            )
            imported += 1
        else:
            immutable = {
                "challenge_type": challenge.challenge_type,
                "field": challenge.field,
                "title": challenge.title,
                "prompt": challenge.prompt,
                "active": challenge.active,
            }
            expected = {key: entry[key] for key in immutable}
            if immutable != expected:
                raise ValueError(
                    f"existing Challenge differs from corpus: {entry['stimulus_group_id']}"
                )
            existing_count += 1
            if challenge.display_summary != entry["display_summary"]:
                challenge.display_summary = entry["display_summary"]
                db.commit()
        challenge_ids.append(str(challenge.challenge_id))
        for source in entry["sources"]:
            existing_source = db.scalar(
                select(ChallengeSource).where(
                    ChallengeSource.challenge_id == challenge.challenge_id,
                    ChallengeSource.source_kind == source["source_kind"],
                    ChallengeSource.source_name == source["source_name"],
                    ChallengeSource.source_role == source["source_role"],
                )
            )
            if existing_source is not None:
                if existing_source.source_url != source["source_url"]:
                    raise ValueError(
                        f"existing provenance differs: {entry['stimulus_group_id']}"
                    )
                continue
            db.add(
                ChallengeSource(
                    source_id=uuid.uuid4(),
                    challenge_id=challenge.challenge_id,
                    **source,
                )
            )
            db.commit()
            sources_added += 1
        if publish and challenge.active:
            thread = db.scalar(
                select(Thread).where(
                    Thread.challenge_id == challenge.challenge_id,
                    Thread.origin_type == "system",
                )
            )
            if thread is None:
                publish_challenge(db, challenge.challenge_id)
                published_count += 1
            else:
                already_published += 1
    return {
        "corpus_id": corpus["corpus_id"],
        "challenges": len(corpus["challenges"]),
        "imported": imported,
        "existing": existing_count,
        "sources_added": sources_added,
        "published": published_count,
        "already_published": already_published,
        "challenge_ids": challenge_ids,
    }
