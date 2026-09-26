"""Publisher metadata extraction and bounded, fail-closed access tests."""

import unittest
from unittest.mock import patch

from app.world_pulse_publisher import (
    MAX_HEAD_BYTES,
    PublisherMetadataClient,
    _HeadMetadata,
    _request,
)
from app.world_pulse_stimulus import derive_publisher_stimulus


class PublisherMetadataTests(unittest.TestCase):
    def test_head_meta_priority_and_article_body_ignored(self):
        parser = _HeadMetadata()
        parser.feed(
            '<html><head><meta name="description" content="A public description with enough'
            ' factual detail to qualify as a candidate for a short context.">'
            '<meta property="og:description" content="A second description that should lose priority.">'
            '<script type="application/ld+json">{"description":"A third description from JSON-LD."}</script>'
            '</head><body><article>Article body must never become metadata.</article></body></html>'
        )
        self.assertTrue(parser.description().startswith("A public description"))
        self.assertNotIn("Article body", parser.description())

    def test_publisher_metadata_fallback_order(self):
        values = (
            ('<meta property="og:description" content="Open Graph description with enough factual detail to be used here.">'
             '<meta name="twitter:description" content="Twitter description with enough factual detail to be used here.">',
             "Open Graph"),
            ('<meta name="twitter:description" content="Twitter description with enough factual detail to be used here.">',
             "Twitter"),
            ('<script type="application/ld+json">{"description":"JSON-LD description with enough factual detail to be used here."}</script>',
             "JSON-LD"),
        )
        for suffix, expected in values:
            parser = _HeadMetadata()
            parser.feed("<html><head>" + suffix + "</head><body>Article body only.</body></html>")
            self.assertTrue(parser.description().startswith(expected))

    def test_jsonld_fallback_and_page_failure_isolated(self):
        html = (
            '<html><head><script type="application/ld+json">'
            '{"@graph":[{"description":"Public JSON-LD metadata contains enough'
            ' information for an article context field."}]}'
            '</script></head><body>Do not parse this body.</body></html>'
        ).encode()
        with patch("app.world_pulse_publisher._robots_allowed", return_value=True), patch(
            "app.world_pulse_publisher._request",
            return_value=(200, {"content-type": "text/html"}, html),
        ):
            value = PublisherMetadataClient().fetch("https://publisher.example/story")
        self.assertIn("Public JSON-LD metadata", value)
        with patch("app.world_pulse_publisher._robots_allowed", return_value=False):
            self.assertIsNone(PublisherMetadataClient().fetch("https://publisher.example/story"))
        with patch("app.world_pulse_publisher._robots_allowed", return_value=True), patch(
            "app.world_pulse_publisher._request",
            return_value=(402, {"content-type": "text/html"}, html),
        ):
            self.assertIsNone(PublisherMetadataClient().fetch("https://publisher.example/paywall"))

    def test_aggregator_page_is_not_mislabelled_publisher(self):
        html = (
            '<html><head><meta name="description" content="An aggregator description'
            ' with sufficient characters to pass metadata validation."></head></html>'
        ).encode()
        with patch("app.world_pulse_publisher._robots_allowed", return_value=True), patch(
            "app.world_pulse_publisher._request",
            return_value=(200, {"content-type": "text/html"}, html),
        ):
            self.assertIsNone(
                PublisherMetadataClient().fetch(
                    "https://news.google.com/rss/articles/id", aggregator=True
                )
            )

    def test_bounded_head_read_does_not_parse_or_store_article_body(self):
        class Response:
            status = 200
            def __init__(self):
                self.payload = b"<html><head><meta name='description' content='safe'></head>" + b"x" * 100_000
                self.position = 0
                self.max_requested = 0
            def getheaders(self):
                return [("content-type", "text/html")]
            def read(self, count):
                self.max_requested = max(self.max_requested, count)
                value = self.payload[self.position:self.position + count]
                self.position += len(value)
                return value
        response = Response()
        class Connection:
            def __init__(self, *args):
                pass
            def request(self, *args, **kwargs):
                pass
            def getresponse(self):
                return response
            def close(self):
                pass
        with patch("app.world_pulse_publisher._pinned_ip", return_value="93.184.215.14"), patch(
            "app.world_pulse_publisher._PinnedHTTPS", Connection
        ):
            status, _, content = _request("https://publisher.example/story", robots=False)
        self.assertEqual(status, 200)
        self.assertLessEqual(len(content), MAX_HEAD_BYTES)
        self.assertLessEqual(response.position, 4096)
        self.assertNotIn(b"x" * 5000, content)

    def test_publisher_summary_requires_corresponding_facts(self):
        summary, source = derive_publisher_stimulus(
            title="Hurricane Polo downgraded but remains powerful off coast of Mexico",
            description="The large storm is driven by warm waters caused by El Niño.",
            source_name="BBC News World", language="en",
        )
        self.assertEqual(source, "publisher_page")
        self.assertIn("warm Pacific waters", summary)
        self.assertLessEqual(len(summary), 800)
        self.assertNotIn("MAS", summary)
        self.assertEqual(
            derive_publisher_stimulus(
                title="Unrelated headline", description="The large storm is driven by warm waters caused by El Niño.",
                source_name="BBC News World", language="en",
            ),
            (None, "unavailable"),
        )

    def test_current_bbc_patterns_are_source_grounded(self):
        examples = (
            ("Ethiopia and Tigray accuse each of launching offensives, fuelling fears of new war",
             "The local authorities reportedly seize Tigray airports following reports of recent drone strikes."),
            ("Dramatic eviction of woman aged 87 highlights Spain housing shortage",
             "The tenant of the flat in Madrid, Maricarmen, was unable to pay the rent set by the firm which recently bought it."),
        )
        for title, description in examples:
            summary, source = derive_publisher_stimulus(
                title=title, description=description, source_name="BBC News World", language="en"
            )
            self.assertEqual(source, "publisher_page")
            self.assertIsNotNone(summary)
            self.assertNotIn(description, summary)
            self.assertNotIn("MAS", summary)
            self.assertNotIn("linked publisher", summary.casefold())


if __name__ == "__main__":
    unittest.main()
