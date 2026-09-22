"""Deterministic World Pulse collection, grouping, selection, and persistence."""

from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid
from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.content import ingest_world_pulse, normalize_source_url, publish_world_pulse
from app.models import Thread, WorldPulseAcquisition, WorldPulseItem
from app.world_pulse_collectors import (
    BoundedHttpClient,
    SourceAdapter,
    SourceCandidate,
)


COLLECTOR_VERSION = "0.1"
TRACKING_PARAMETERS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref_src",
    "at_campaign",
    "at_medium",
}
STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "has", "in", "is", "it", "of", "on", "that", "the", "to", "with",
    "after", "new", "says", "over", "into", "latest", "live", "update",
}


@dataclass(frozen=True)
class Candidate:
    adapter: str
    profile: str
    source_type: str
    source_name: str
    title: str
    summary: str
    source_url: str
    normalized_source_url: str
    published_at: datetime
    external_id: str | None
    language: str
    source_rank: int | None
    acquired_at: datetime
    cluster_key: str | None = None


@dataclass(frozen=True)
class SelectedCandidate:
    candidate: Candidate
    score: int
    score_components: dict[str, int]


@dataclass
class CollectionBatch:
    attempted: int
    succeeded: int
    candidates: list[SourceCandidate]
    errors: list[dict[str, str]]
    source_results: list[dict[str, object]]


def collect_from_sources(
    collectors: list[SourceAdapter], client: BoundedHttpClient
) -> CollectionBatch:
    candidates: list[SourceCandidate] = []
    errors: list[dict[str, str]] = []
    source_results: list[dict[str, object]] = []
    succeeded = 0
    for collector in collectors:
        try:
            collected = collector.collect(client)
            candidates.extend(collected)
            succeeded += 1
            source_results.append(
                {
                    "adapter": collector.adapter_id,
                    "profile": collector.profile,
                    "reachable": True,
                    "result_count": len(collected),
                }
            )
        except Exception as exc:  # A collector boundary must be fail-soft.
            error = {
                "adapter": collector.adapter_id,
                "profile": collector.profile,
                "error": f"{type(exc).__name__}: {exc}",
            }
            errors.append(error)
            source_results.append(
                {
                    "adapter": collector.adapter_id,
                    "profile": collector.profile,
                    "reachable": False,
                    "result_count": 0,
                    "error": error["error"],
                }
            )
    return CollectionBatch(
        len(collectors), succeeded, candidates, errors, source_results
    )


def _clean_text(value: str, *, maximum: int) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError("candidate text is empty or too long")
    if any(unicodedata.category(character) == "Cc" for character in normalized):
        raise ValueError("candidate text contains control characters")
    return normalized


def normalize_candidate(raw: SourceCandidate, acquired_at: datetime) -> Candidate:
    if raw.language not in {"en", "ja", "zh", "mixed", "unknown"}:
        raise ValueError("unsupported candidate language")
    if raw.source_type not in {
        "news", "google_trends", "x_trend", "official_release", "other"
    }:
        raise ValueError("unsupported candidate source_type")
    if acquired_at.tzinfo is None:
        raise ValueError("acquired_at must include a timezone")
    parsed = urlsplit(raw.source_url.strip())
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_PARAMETERS
    ]
    cleaned_url = urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )
    normalized_url = normalize_source_url(cleaned_url)
    title = _clean_text(raw.title, maximum=300)
    context = raw.context or title
    summary = _clean_text(context, maximum=1_000)
    published = raw.published_at or acquired_at
    if published.tzinfo is None:
        raise ValueError("published_at must include a timezone")
    external_id = raw.external_id.strip() if raw.external_id else None
    if external_id and len(external_id) > 255:
        raise ValueError("external_id is too long")
    rank = raw.source_rank
    if rank is not None and rank < 1:
        raise ValueError("source_rank must be positive")
    return Candidate(
        adapter=_clean_text(raw.adapter, maximum=100),
        profile=_clean_text(raw.profile, maximum=50),
        source_type=raw.source_type,
        source_name=_clean_text(raw.source_name, maximum=200),
        title=title,
        summary=summary,
        source_url=cleaned_url,
        normalized_source_url=normalized_url,
        published_at=published.astimezone(UTC),
        external_id=external_id,
        language=raw.language,
        source_rank=rank,
        acquired_at=acquired_at.astimezone(UTC),
    )


def normalize_candidates(
    candidates: list[SourceCandidate], acquired_at: datetime
) -> tuple[list[Candidate], int]:
    normalized: list[Candidate] = []
    rejected = 0
    for candidate in candidates:
        try:
            normalized.append(normalize_candidate(candidate, acquired_at))
        except (TypeError, ValueError):
            rejected += 1
    return normalized, rejected


def deduplicate_candidates(candidates: list[Candidate]) -> tuple[list[Candidate], int]:
    seen_external: set[tuple[str, str]] = set()
    seen_urls: set[str] = set()
    unique: list[Candidate] = []
    duplicates = 0
    ordered = sorted(
        candidates,
        key=lambda item: (
            item.source_rank if item.source_rank is not None else 2_147_483_647,
            item.adapter,
            item.normalized_source_url,
        ),
    )
    for candidate in ordered:
        external_key = (
            (candidate.source_type, candidate.external_id)
            if candidate.external_id is not None
            else None
        )
        if candidate.normalized_source_url in seen_urls or (
            external_key is not None and external_key in seen_external
        ):
            duplicates += 1
            continue
        seen_urls.add(candidate.normalized_source_url)
        if external_key is not None:
            seen_external.add(external_key)
        unique.append(candidate)
    return unique, duplicates


def _title_tokens(title: str) -> frozenset[str]:
    text = unicodedata.normalize("NFKC", title).casefold()
    tokens = re.findall(r"[^\W_]+", text, flags=re.UNICODE)
    return frozenset(
        token for token in tokens if len(token) > 2 and token not in STOPWORDS
    )


def _same_obvious_event(left: Candidate, right: Candidate) -> bool:
    if abs(left.published_at - right.published_at) > timedelta(hours=48):
        return False
    left_tokens, right_tokens = _title_tokens(left.title), _title_tokens(right.title)
    if not left_tokens or not right_tokens:
        return False
    if left_tokens == right_tokens:
        return True
    union = left_tokens | right_tokens
    return len(left_tokens) >= 5 and len(right_tokens) >= 5 and (
        len(left_tokens & right_tokens) / len(union) >= 0.85
    )


def group_candidates(candidates: list[Candidate]) -> list[Candidate]:
    parents = list(range(len(candidates)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parents[max(left_root, right_root)] = min(left_root, right_root)

    for left in range(len(candidates)):
        for right in range(left + 1, len(candidates)):
            if candidates[left].adapter == candidates[right].adapter:
                continue
            if _same_obvious_event(candidates[left], candidates[right]):
                union(left, right)

    groups: dict[int, list[int]] = {}
    for index in range(len(candidates)):
        groups.setdefault(find(index), []).append(index)
    output = list(candidates)
    for indexes in groups.values():
        if len(indexes) < 2:
            continue
        common = set(_title_tokens(candidates[indexes[0]].title))
        for index in indexes[1:]:
            common &= _title_tokens(candidates[index].title)
        material = "|".join(sorted(common))
        if not material:
            continue
        cluster_key = "title-" + hashlib.sha256(material.encode()).hexdigest()[:24]
        for index in indexes:
            output[index] = replace(output[index], cluster_key=cluster_key)
    return output


def select_candidates(
    candidates: list[Candidate], *, now: datetime, limit: int = 10
) -> list[SelectedCandidate]:
    if now.tzinfo is None:
        raise ValueError("now must include a timezone")
    if not 0 <= limit <= 100:
        raise ValueError("limit must be between 0 and 100")
    cluster_sources: dict[str, set[str]] = {}
    for candidate in candidates:
        if candidate.cluster_key:
            cluster_sources.setdefault(candidate.cluster_key, set()).add(candidate.adapter)

    scored: list[SelectedCandidate] = []
    for candidate in candidates:
        rank_score = max(0, 51 - min(candidate.source_rank or 51, 51))
        age_hours = max(
            0, int((now.astimezone(UTC) - candidate.published_at).total_seconds() // 3600)
        )
        recency_score = max(0, 48 - age_hours)
        cross_source_score = min(
            30,
            15 * max(0, len(cluster_sources.get(candidate.cluster_key or "", set())) - 1),
        )
        components = {
            "source_rank": rank_score,
            "recency": recency_score,
            "cross_source": cross_source_score,
        }
        scored.append(
            SelectedCandidate(candidate, sum(components.values()), components)
        )
    scored.sort(
        key=lambda selected: (
            -selected.score,
            -selected.candidate.published_at.timestamp(),
            selected.candidate.adapter,
            selected.candidate.normalized_source_url,
        )
    )

    chosen: list[SelectedCandidate] = []
    chosen_urls: set[str] = set()
    source_counts: Counter[str] = Counter()
    language_counts: Counter[str] = Counter()
    cluster_counts: Counter[str] = Counter()

    def add(selected: SelectedCandidate) -> bool:
        candidate = selected.candidate
        if candidate.normalized_source_url in chosen_urls:
            return False
        if source_counts[candidate.adapter] >= 4:
            return False
        if language_counts[candidate.language] >= 6:
            return False
        if candidate.cluster_key and cluster_counts[candidate.cluster_key] >= 2:
            return False
        chosen.append(selected)
        chosen_urls.add(candidate.normalized_source_url)
        source_counts[candidate.adapter] += 1
        language_counts[candidate.language] += 1
        if candidate.cluster_key:
            cluster_counts[candidate.cluster_key] += 1
        return True

    for language in sorted({item.candidate.language for item in scored}):
        first = next((item for item in scored if item.candidate.language == language), None)
        if first is not None and len(chosen) < limit:
            add(first)
    for selected in scored:
        if len(chosen) >= limit:
            break
        add(selected)
    return chosen


def _existing_item(db: Session, candidate: Candidate) -> WorldPulseItem | None:
    clauses = [WorldPulseItem.normalized_source_url == candidate.normalized_source_url]
    if candidate.external_id is not None:
        clauses.append(
            (WorldPulseItem.source_type == candidate.source_type)
            & (WorldPulseItem.external_id == candidate.external_id)
        )
    return db.scalar(select(WorldPulseItem).where(or_(*clauses)).limit(1))


def _record_acquisition(
    db: Session,
    item: WorldPulseItem,
    selected: SelectedCandidate,
    selection_date: date,
) -> None:
    candidate = selected.candidate
    existing = db.scalar(
        select(WorldPulseAcquisition).where(
            WorldPulseAcquisition.pulse_id == item.pulse_id
        )
    )
    if existing is not None:
        return
    db.add(
        WorldPulseAcquisition(
            acquisition_id=uuid.uuid4(),
            pulse_id=item.pulse_id,
            source_adapter=candidate.adapter,
            source_profile=candidate.profile,
            acquired_at=candidate.acquired_at,
            selection_date=selection_date,
            source_rank=candidate.source_rank,
            selection_score=selected.score,
            score_components=selected.score_components,
            collector_version=COLLECTOR_VERSION,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()


def _selection_view(selected: SelectedCandidate) -> dict[str, object]:
    candidate = selected.candidate
    return {
        "adapter": candidate.adapter,
        "profile": candidate.profile,
        "title": candidate.title,
        "language": candidate.language,
        "source_url": candidate.source_url,
        "source_rank": candidate.source_rank,
        "cluster_key": candidate.cluster_key,
        "selection_score": selected.score,
        "score_components": selected.score_components,
    }


def run_pipeline(
    db: Session,
    collectors: list[SourceAdapter],
    *,
    dry_run: bool,
    now: datetime | None = None,
    selection_date: date | None = None,
    limit: int = 10,
    client: BoundedHttpClient | None = None,
) -> dict[str, object]:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    selection_date = selection_date or now.date()
    client = client or BoundedHttpClient()
    batch = collect_from_sources(collectors, client)
    normalized, malformed = normalize_candidates(batch.candidates, now)
    unique, duplicates = deduplicate_candidates(normalized)
    grouped = group_candidates(unique)

    new_candidates: list[Candidate] = []
    existing = 0
    for candidate in grouped:
        item = _existing_item(db, candidate)
        if item is None:
            new_candidates.append(candidate)
            continue
        has_acquisition = db.scalar(
            select(WorldPulseAcquisition.acquisition_id).where(
                WorldPulseAcquisition.pulse_id == item.pulse_id
            )
        ) is not None
        has_thread = db.scalar(
            select(Thread.thread_id).where(
                Thread.world_pulse_item_id == item.pulse_id,
                Thread.origin_type == "world_pulse",
            )
        ) is not None
        if has_acquisition and has_thread:
            existing += 1
        else:
            # Recover an interrupted earlier run without duplicating domain rows.
            new_candidates.append(candidate)
    already_selected_today = db.scalar(
        select(func.count())
        .select_from(WorldPulseAcquisition)
        .join(
            Thread,
            (Thread.world_pulse_item_id == WorldPulseAcquisition.pulse_id)
            & (Thread.origin_type == "world_pulse"),
        )
        .where(WorldPulseAcquisition.selection_date == selection_date)
    ) or 0
    remaining = max(0, limit - already_selected_today)
    selected = select_candidates(new_candidates, now=now, limit=remaining)

    report: dict[str, object] = {
        "dry_run": dry_run,
        "selection_date": selection_date.isoformat(),
        "sources_attempted": batch.attempted,
        "sources_succeeded": batch.succeeded,
        "source_results": batch.source_results,
        "candidates": len(batch.candidates),
        "malformed_rejected": malformed,
        "duplicates_removed": duplicates,
        "already_present": existing,
        "clusters": len({item.cluster_key for item in grouped if item.cluster_key}),
        "selected": len(selected),
        "ingested": 0,
        "published": 0,
        "errors": batch.errors,
        "items": [_selection_view(item) for item in selected],
    }
    if dry_run:
        return report

    ingested = published = 0
    persistence_errors: list[dict[str, str]] = []
    for choice in selected:
        candidate = choice.candidate
        try:
            item = _existing_item(db, candidate)
            if item is None:
                item = ingest_world_pulse(
                    db,
                    title=candidate.title,
                    summary=candidate.summary,
                    language=candidate.language,
                    published_at=candidate.published_at,
                    source_type=candidate.source_type,
                    source_url=candidate.source_url,
                    source_name=candidate.source_name,
                    external_id=candidate.external_id,
                    cluster_key=candidate.cluster_key,
                )
                ingested += 1
            _record_acquisition(db, item, choice, selection_date)
            thread = db.scalar(
                select(Thread).where(Thread.world_pulse_item_id == item.pulse_id)
            )
            if thread is None:
                publish_world_pulse(db, item.pulse_id)
                published += 1
        except ValueError as exc:
            db.rollback()
            persistence_errors.append(
                {"adapter": candidate.adapter, "error": str(exc)}
            )
    report["ingested"] = ingested
    report["published"] = published
    report["errors"] = [*batch.errors, *persistence_errors]
    return report
