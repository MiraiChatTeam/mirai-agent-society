"""Offline source-class balancing and Google News profile tests."""

import unittest
from datetime import UTC, datetime, timedelta

from app.world_pulse_acquisition import (
    SOURCE_CLASSES,
    filter_current_candidates,
    normalize_candidate,
    select_candidates,
)
from app.world_pulse_collectors import SourceCandidate, configured_collectors


NOW = datetime(2026, 9, 23, 12, tzinfo=UTC)


def candidate(index, *, kind="news", profile="global-en", adapter="publisher",
              language="en", age=0, context=None):
    title = f"Launch report {index}" if context else f"News report {index}"
    return normalize_candidate(
        SourceCandidate(
            adapter=adapter, profile=profile, source_type=kind,
            source_name=adapter, title=title,
            source_url=f"https://example.test/{kind}/{adapter}/{index}",
            published_at=NOW - timedelta(hours=age),
            external_id=None, language=language, context=context,
            source_rank=index + 1,
        ),
        NOW,
    )


class SourceBalanceTests(unittest.TestCase):
    def test_google_news_en_ja_reuse_rss_adapter_with_explicit_provenance(self):
        configured = {item.adapter_id: item for item in configured_collectors()}
        for adapter, profile, language, query in (
            ("google-news-en", "global-en", "en", "ceid=US:en"),
            ("google-news-ja", "japan-ja", "ja", "ceid=JP:ja"),
            ("google-news-zh", "china-zh", "zh", "ceid=CN:zh-Hans"),
        ):
            item = configured[adapter]
            self.assertEqual((item.profile, item.language, item.source_type),
                             (profile, language, "news"))
            self.assertIn(query, item.feed_url)
            self.assertEqual(item.allowed_hosts, frozenset({"news.google.com"}))
            self.assertIn(item, configured_collectors(profile))
        self.assertEqual(SOURCE_CLASSES, {"news": "NEWS", "google_trends": "ATTENTION"})

    def test_news_first_two_trends_max_one_per_profile_and_deterministic(self):
        news = [
            candidate(i, adapter=f"news-{i // 3}", language=("en", "ja", "zh")[i % 3],
                      profile=("global-en", "japan-ja", "china-zh")[i % 3])
            for i in range(12)
        ]
        trends = [
            candidate(i, kind="google_trends", adapter=f"trends-{profile}",
                      profile=profile, language=language)
            for profile, language in (("global-en", "en"), ("japan-ja", "ja"))
            for i in range(4)
        ]
        all_candidates = news + trends
        chosen = select_candidates(all_candidates, now=NOW, limit=10)
        reverse = select_candidates(list(reversed(all_candidates)), now=NOW, limit=10)
        self.assertEqual([item.candidate.source_url for item in chosen],
                         [item.candidate.source_url for item in reverse])
        self.assertEqual(len(chosen), 10)
        trend_items = [item for item in chosen if item.candidate.source_type == "google_trends"]
        self.assertEqual(len(trend_items), 2)
        self.assertEqual({item.candidate.profile for item in trend_items},
                         {"global-en", "japan-ja"})
        self.assertEqual(sum(item.candidate.source_type == "news" for item in chosen), 8)

    def test_fewer_than_limit_and_stale_news_never_fills(self):
        fresh = [candidate(i, adapter=f"news-{i}") for i in range(3)]
        stale = candidate(9, adapter="stale", age=96)
        trends = [
            candidate(i, kind="google_trends", adapter="trends-us")
            for i in range(4)
        ]
        current, stale_counts, _ = filter_current_candidates(
            fresh + [stale] + trends, now=NOW
        )
        chosen = select_candidates(current, now=NOW, limit=10)
        self.assertEqual(stale_counts["stale"], 1)
        self.assertEqual(len(chosen), 4)
        self.assertEqual(sum(item.candidate.source_type == "google_trends" for item in chosen), 1)
        self.assertNotIn("stale", {item.candidate.adapter for item in chosen})

    def test_source_grounded_news_context_is_preferred_within_source(self):
        bare = candidate(0, adapter="same-news")
        grounded = candidate(
            1, adapter="same-news",
            context="Mission controllers confirmed a successful launch.",
        )
        self.assertIsNotNone(grounded.stimulus_summary)
        chosen = select_candidates([bare, grounded], now=NOW, limit=1)
        self.assertEqual(chosen[0].candidate.title, grounded.title)

    def test_small_limit_keeps_news_first_but_allows_attention_only(self):
        news = candidate(0, adapter="news")
        trend = candidate(0, kind="google_trends", adapter="trends")
        first = select_candidates([trend, news], now=NOW, limit=1)
        self.assertEqual(first[0].candidate.source_type, "news")
        attention_only = select_candidates([trend], now=NOW, limit=1)
        self.assertEqual(len(attention_only), 1)
        self.assertEqual(attention_only[0].candidate.source_type, "google_trends")


if __name__ == "__main__":
    unittest.main()
