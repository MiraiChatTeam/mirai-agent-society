"""Conservative cross-source NEWS event suppression tests."""

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from app.world_pulse_acquisition import (
    group_candidates,
    normalize_candidate,
    suppress_obvious_cross_source_events,
)
from app.world_pulse_collectors import SourceCandidate


NOW = datetime(2026, 9, 23, 12, tzinfo=UTC)


def news(title, adapter, *, age=0, context=None, rank=1):
    return normalize_candidate(
        SourceCandidate(
            adapter=adapter,
            profile="global-en",
            source_type="news",
            source_name=adapter,
            title=title,
            source_url=f"https://example.test/{adapter}/{rank}",
            published_at=NOW - timedelta(hours=age),
            external_id=None,
            language="en",
            context=context,
            source_rank=rank,
        ),
        NOW,
    )


class EventDedupTests(unittest.TestCase):
    def test_bbc_google_news_suffix_duplicate_prefers_direct_publisher(self):
        direct = news(
            "Trump meets US-backed Venezuelan president for first time since Maduro seized",
            "bbc-world-rss", rank=10,
        )
        aggregate = news(
            "Trump meets US-backed Venezuelan president for first time since Maduro seized - BBC",
            "google-news-en", rank=1,
        )
        grouped = group_candidates([aggregate, direct])
        first, suppressed = suppress_obvious_cross_source_events(grouped)
        reverse, reverse_count = suppress_obvious_cross_source_events(
            group_candidates([direct, aggregate])
        )
        self.assertEqual(suppressed, reverse_count)
        self.assertEqual(suppressed, 1)
        self.assertEqual(first[0].adapter, reverse[0].adapter)
        self.assertEqual(first[0].adapter, "bbc-world-rss")

    def test_short_headline_with_bbc_news_dash_suffix_is_suppressed(self):
        direct = news("World leaders meet today", "bbc-world-rss")
        aggregate = news("World leaders meet today — BBC News", "google-news-en")
        kept, suppressed = suppress_obvious_cross_source_events(
            group_candidates([aggregate, direct])
        )
        self.assertEqual(suppressed, 1)
        self.assertEqual([item.adapter for item in kept], ["bbc-world-rss"])

    def test_context_breaks_tie_between_two_direct_publishers(self):
        bare = news("Major lunar mission launches successfully today", "publisher-a")
        grounded = news(
            "Major lunar mission launches successfully today",
            "publisher-b", context="Mission controllers confirmed a successful launch.",
        )
        kept, suppressed = suppress_obvious_cross_source_events(
            group_candidates([bare, grounded])
        )
        self.assertEqual(suppressed, 1)
        self.assertEqual(kept[0].adapter, "publisher-b")

    def test_related_but_different_events_and_distant_dates_stay_separate(self):
        first = news("Tigray forces seize main airport from police", "bbc-world-rss")
        other = news("Tigray government reopens airport after police talks", "google-news-en")
        distant = replace(
            news("Tigray forces seize main airport from police - BBC", "google-news-ja"),
            published_at=NOW - timedelta(days=4),
        )
        grouped = group_candidates([first, other, distant])
        kept, suppressed = suppress_obvious_cross_source_events(grouped)
        self.assertEqual((len(kept), suppressed), (3, 0))


if __name__ == "__main__":
    unittest.main()
