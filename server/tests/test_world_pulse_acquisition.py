import urllib.error
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from app.world_pulse_acquisition import (
    collect_from_sources,
    deduplicate_candidates,
    group_candidates,
    normalize_candidate,
    normalize_candidates,
    select_candidates,
)
from app.world_pulse_collectors import (
    BoundedHttpClient,
    RSSCollector,
    SourceCandidate,
)


NOW = datetime(2026, 9, 22, 2, 0, tzinfo=UTC)
FIXTURES = Path(__file__).parent / "fixtures"


class FixtureClient:
    def __init__(self, filename: str):
        self.payload = (FIXTURES / filename).read_bytes()

    def get(self, url, *, allowed_hosts, timeout=10, max_bytes=1_000_000):
        return self.payload


class BrokenCollector:
    adapter_id = "broken"
    profile = "global-en"

    def collect(self, client):
        raise TimeoutError("fixture timeout")


class StaticCollector:
    adapter_id = "static"
    profile = "global-en"

    def __init__(self, candidates):
        self.candidates = candidates

    def collect(self, client):
        return self.candidates


def raw(
    title: str,
    url: str,
    *,
    adapter: str = "fixture-a",
    language: str = "en",
    rank: int = 1,
    external_id: str | None = None,
) -> SourceCandidate:
    return SourceCandidate(
        adapter=adapter,
        profile={"en": "global-en", "ja": "japan-ja", "zh": "china-zh"}.get(language, "global-en"),
        source_type="news",
        source_name=adapter,
        title=title,
        source_url=url,
        published_at=NOW,
        external_id=external_id,
        language=language,
        context=None,
        source_rank=rank,
    )


class CollectorTests(unittest.TestCase):
    def test_rss_adapter_and_normalization(self):
        collector = RSSCollector(
            "fixture-global", "global-en", "https://fixture.example/feed",
            "news", "Fixture Global", "en", frozenset({"fixture.example"}),
        )
        candidates = collector.collect(FixtureClient("global_en.xml"))
        self.assertEqual(len(candidates), 3)
        normalized = normalize_candidate(candidates[0], NOW)
        self.assertEqual(normalized.summary, "Mission controllers confirmed a successful launch.")
        self.assertNotIn("utm_source", normalized.normalized_source_url)

    def test_source_failure_is_isolated(self):
        candidate = raw("Valid fixture title", "https://example.test/item")
        batch = collect_from_sources(
            [BrokenCollector(), StaticCollector([candidate])], FixtureClient("global_en.xml")
        )
        self.assertEqual((batch.attempted, batch.succeeded, len(batch.candidates)), (2, 1, 1))
        self.assertEqual(batch.errors[0]["adapter"], "broken")

    def test_google_trends_items_get_stable_per_query_identity(self):
        collector = RSSCollector(
            "fixture-trends", "global-en", "https://fixture.example/feed?geo=US",
            "google_trends", "Fixture Trends", "en", frozenset({"fixture.example"}),
        )
        candidates = collector.collect(FixtureClient("global_en.xml"))
        self.assertEqual(candidates[0].source_url, candidates[1].source_url)
        self.assertNotEqual(candidates[0].source_url, candidates[2].source_url)
        self.assertEqual(candidates[0].external_id, candidates[1].external_id)
        self.assertIn("trends.google.com/trends/explore", candidates[0].source_url)

    def test_network_timeout_is_wrapped(self):
        with patch("urllib.request.build_opener") as build:
            build.return_value.open.side_effect = urllib.error.URLError("timed out")
            with self.assertRaisesRegex(RuntimeError, "source request failed"):
                BoundedHttpClient().get(
                    "https://feeds.example.test/rss",
                    allowed_hosts=frozenset({"feeds.example.test"}),
                )

    def test_response_size_header_is_rejected(self):
        class Response:
            headers = {"Content-Length": "1000001"}
            def __enter__(self): return self
            def __exit__(self, *args): return False

        with patch("urllib.request.build_opener") as build:
            build.return_value.open.return_value = Response()
            with self.assertRaisesRegex(ValueError, "size limit"):
                BoundedHttpClient().get(
                    "https://feeds.example.test/rss",
                    allowed_hosts=frozenset({"feeds.example.test"}),
                )


class PipelineTests(unittest.TestCase):
    def test_url_and_external_id_deduplication(self):
        candidates = [
            normalize_candidate(raw("One", "https://example.test/a?utm_source=x", external_id="a:1"), NOW),
            normalize_candidate(raw("One again", "https://example.test/a", external_id="a:2"), NOW),
            normalize_candidate(raw("External duplicate", "https://example.test/b", external_id="a:1"), NOW),
        ]
        unique, duplicates = deduplicate_candidates(candidates)
        self.assertEqual((len(unique), duplicates), (1, 2))

    def test_grouping_and_conservative_non_grouping(self):
        candidates = [
            normalize_candidate(raw("Major lunar mission launches successfully today", "https://one.test/a", adapter="one"), NOW),
            normalize_candidate(raw("Major lunar mission launches successfully today", "https://two.test/b", adapter="two"), NOW),
            normalize_candidate(raw("Regional football final ends after penalties", "https://three.test/c", adapter="three"), NOW),
        ]
        grouped = group_candidates(candidates)
        self.assertIsNotNone(grouped[0].cluster_key)
        self.assertEqual(grouped[0].cluster_key, grouped[1].cluster_key)
        self.assertIsNone(grouped[2].cluster_key)

    def test_selection_is_deterministic_diverse_and_has_no_filler(self):
        candidates = []
        for index in range(8):
            candidates.append(normalize_candidate(raw(f"English item number {index}", f"https://en.test/{index}", adapter="en-source", rank=index + 1), NOW))
        candidates.extend([
            normalize_candidate(raw("日本語の科学ニュース", "https://ja.test/1", adapter="ja-source", language="ja"), NOW),
            normalize_candidate(raw("中文科学新闻项目", "https://zh.test/1", adapter="zh-source", language="zh"), NOW),
        ])
        first = select_candidates(candidates, now=NOW, limit=10)
        second = select_candidates(list(reversed(candidates)), now=NOW, limit=10)
        self.assertEqual(
            [item.candidate.normalized_source_url for item in first],
            [item.candidate.normalized_source_url for item in second],
        )
        self.assertLessEqual(sum(item.candidate.adapter == "en-source" for item in first), 4)
        self.assertEqual({item.candidate.language for item in first}, {"en", "ja", "zh"})
        self.assertEqual(len(first), 6)

    def test_malformed_candidates_are_rejected(self):
        valid = raw("Valid item", "https://example.test/valid")
        invalid = raw("   ", "https://example.test/invalid")
        normalized, rejected = normalize_candidates([valid, invalid], NOW)
        self.assertEqual((len(normalized), rejected), (1, 1))


if __name__ == "__main__":
    unittest.main()
