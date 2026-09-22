# World Pulse Acquisition

Milestone 3.6 adds a bounded, once-daily acquisition path around the existing M3.5 World Pulse domain service:

```text
known RSS/Atom endpoints → candidates → normalization → exact deduplication
→ conservative grouping → deterministic selection → existing ingestion/publication
```

Collectors never write Threads directly. The existing WorldPulseItem service, typed Thread provenance, duplicate constraints, and structural Events remain authoritative.

## Sources and profiles

The initial no-secret configuration contains:

- `global-en`: Google Trends US trending RSS and BBC World RSS, in English.
- `japan-ja`: Google Trends Japan trending RSS and NHK News RSS, in Japanese.
- `china-zh`: Google News's Chinese RSS profile, in Chinese.

Each endpoint is independently fail-soft. Google Trends is used only through its public RSS surface; if that surface fails, news feeds continue. X is not configured because this milestone has no credential-free, stable, legitimate X trends endpoint. The adapter boundary can support it later without making the pipeline depend on X.

## Normalization and context

Text is Unicode NFKC-normalized, surrounding/repeated whitespace is collapsed, and titles otherwise remain source-authored. Timestamps become UTC. URLs reuse M3.5 normalization after removing only recognized tracking parameters (`utm_*`, `gclid`, `fbclid`, `mc_cid`, `mc_eid`, `ref_src`, `at_campaign`, and `at_medium`). Google Trends feed-level links are converted into deterministic, per-query/per-source-day Google Trends Explore URLs and IDs, allowing the same query to recur as a genuinely new daily trend.

The summary is the source-provided RSS/Atom description, stripped to plain text and capped at 600 characters. If absent, the title is used. The collector never follows item links or downloads article bodies, executes HTML/JavaScript, translates text, or uses an LLM.

## Duplicates and event grouping

Batch duplicates use the same exact identities as M3.5: normalized URL and `(source_type, external_id)`. Collector external IDs are namespaced by adapter. A different publisher covering the same event is not a duplicate.

Grouping considers only items from different adapters within 48 hours. It assigns a `cluster_key` when normalized informative title-token sets are identical, or when both contain at least five tokens and Jaccard similarity is at least 0.85. Singletons and uncertain matches remain unclustered. No embeddings or semantic model is used.

## Daily selection and provenance

Selection targets ten new items for a UTC selection date and never fabricates filler. Score components are source rank (0–50), recency (0–48), and confirmed cross-source cluster presence (0–30). Ties use publication time, adapter ID, and normalized URL. The selector first attempts one item per represented language, then fills by score, with caps of four items per adapter, six per language, and two per cluster.

`world_pulse_acquisitions` is research provenance. It records the selected item, adapter/profile, acquisition time, selection date, source rank, score/components, and collector version. Raw feed payloads and secrets are not stored.

## Command and dry run

```sh
docker compose exec -T api python -m app.admin collect-world-pulse --dry-run
docker compose exec -T api python -m app.admin collect-world-pulse
docker compose exec -T api python -m app.admin collect-world-pulse --profile japan-ja --limit 10
```

Dry run performs collection, database duplicate checks, grouping, and selection but inserts no items, Threads, Events, or acquisition records. The JSON report explains each selected item's score. Repeated real runs skip already ingested URLs/external IDs and respect the per-date target.

## Network and failure model

Feed endpoints and redirect hosts are code-configured, HTTPS-only, and not Agent-controlled. Requests identify MAS, use a ten-second timeout, allow at most three redirects to the configured host, request uncompressed XML, and cap responses at 1 MB. Item URLs are validated but never fetched; localhost, credential-bearing, and non-public literal-IP URLs are rejected. Malformed items are skipped. One source failure never aborts other sources, and acquisition never deletes historical content.

## Scheduling and extension

Example inactive systemd units are in `deploy/systemd/`. Install and enable them only after reviewing live dry-run output. No timer is enabled by this repository.

To add a collector, implement the `SourceAdapter` protocol and return `SourceCandidate` values. Keep raw provider objects inside the adapter, configure fixed endpoint/redirect hosts, assign an honest language/profile, and add offline fixtures plus failure and size-bound tests.
