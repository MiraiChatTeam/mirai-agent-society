"""Conservative, offline World Pulse stimulus derivation tests."""

import unittest

from app.world_pulse_stimulus import derive_stimulus, trend_context


class StimulusSummaryTests(unittest.TestCase):
    def test_rss_description_becomes_short_paraphrased_context(self):
        description = (
            "The talks are the first since a ceasefire collapsed in June, "
            "with the sides exchanging fire intermittently."
        )
        summary, source = derive_stimulus(
            title="US and Iran hold first talks since June",
            description=description,
            source_name="BBC News World",
            source_type="news",
            adapter="bbc-world-rss",
            profile="global-en",
            language="en",
        )
        self.assertEqual(source, "feed_metadata")
        self.assertIsNotNone(summary)
        self.assertGreaterEqual(len(summary.split()), 15)
        self.assertLessEqual(len(summary.split()), 100)
        self.assertIn("June ceasefire collapse", summary)
        self.assertNotIn("ceasefire collapsed in June, with the sides", summary)
        self.assertNotIn(description, summary)
        self.assertNotIn("MAS", summary)
        self.assertNotIn("linked source", summary)

    def test_feed_precedes_publisher_and_requires_matching_details(self):
        cases = (
            (
                "OpenAI agent 'infiltrated' Australian government website, PM says",
                'Albanese said he expressed "concern" to OpenAI founder Sam Altman, '
                'after authorities were informed three months after the breach in June.',
            ),
            (
                "AI superpower ambitions take centre stage as Trump and Xi meet",
                "The US and China are vying for AI supremacy while seeking to keep it under human control.",
            ),
        )
        for title, description in cases:
            with self.subTest(title=title):
                summary, source = derive_stimulus(
                    title=title, description=description, source_name="BBC News World",
                    source_type="news", adapter="bbc-world-rss",
                    profile="global-en", language="en",
                    publisher_page_fetch=lambda _url: self.fail("feed must take precedence"),
                    source_url="https://publisher.example/story",
                )
                self.assertEqual(source, "feed_metadata")
                self.assertEqual(summary.count(". "), 1)
                self.assertNotIn(description, summary)
                self.assertNotIn("MAS", summary)
                self.assertEqual(
                    derive_stimulus(
                        title=title, description="Unrelated public metadata.",
                        source_name="BBC News World", source_type="news",
                        adapter="bbc-world-rss", profile="global-en", language="en",
                    ),
                    (None, "unavailable"),
                )

    def test_trend_context_is_not_a_news_claim_and_is_bounded(self):
        summary, source = trend_context("bitcoin", profile="global-en", language="en")
        self.assertEqual(source, "trend_context")
        self.assertIn("search interest, not a verified news event", summary)
        self.assertGreaterEqual(len(summary.split()), 15)
        self.assertLessEqual(len(summary.split()), 100)
        self.assertLessEqual(len(summary), 800)
        self.assertNotIn("MAS", summary)
        for language, profile in (("ja", "japan-ja"), ("zh", "china-zh")):
            localized, localized_source = trend_context("test", profile=profile, language=language)
            self.assertEqual(localized_source, "trend_context")
            self.assertLessEqual(len(localized), 800)
            self.assertNotIn("MAS", localized)

    def test_insufficient_or_unrelated_metadata_stays_unavailable(self):
        common = dict(
            title="A topic with no independent detail",
            source_name="Public News",
            source_type="news",
            adapter="bbc-world-rss",
            profile="global-en",
            language="en",
        )
        self.assertEqual(derive_stimulus(description=None, **common), (None, "unavailable"))
        self.assertEqual(
            derive_stimulus(description="This is unrelated source metadata.", **common),
            (None, "unavailable"),
        )
        self.assertEqual(
            derive_stimulus(description="Mission controllers confirmed a successful launch.", **common),
            (None, "unavailable"),
        )

    def test_optional_publisher_page_failure_is_isolated(self):
        def failing_fetch(_url):
            raise TimeoutError("publisher unavailable")

        summary, source = derive_stimulus(
            title="Unclear public report",
            description="No usable facts in the feed metadata.",
            source_name="Google News",
            source_type="news",
            adapter="google-news-en",
            profile="global-en",
            language="en",
            publisher_page_fetch=failing_fetch,
            source_url="https://publisher.example/report",
        )
        self.assertEqual((summary, source), (None, "unavailable"))

    def test_oversized_context_is_not_persistable(self):
        self.assertEqual(
            trend_context("x" * 1000, profile="global-en", language="en"),
            (None, "unavailable"),
        )


if __name__ == "__main__":
    unittest.main()
