"""Conservative cross-source relation classifier tests."""

import unittest
import uuid
from datetime import UTC, datetime, timedelta

from app.models import WorldPulseItem
from app.world_pulse_event_relations import same_event


NOW = datetime(2026, 9, 23, 12, tzinfo=UTC)


def item(title, source, *, hours=0, source_type="news", language="en"):
    return WorldPulseItem(
        pulse_id=uuid.uuid4(), title=title, source_name=source,
        published_at=NOW + timedelta(hours=hours),
        source_type=source_type, language=language,
    )


class EventRelationTests(unittest.TestCase):
    def test_distinct_reports_of_same_airport_seizure_are_related(self):
        direct = item("Tigray forces seize airport in northern Ethiopia", "BBC")
        aggregate = item("Ethiopia: Tigray fighters capture airport in northern region", "Google News")
        self.assertTrue(same_event(direct, aggregate))
        self.assertTrue(same_event(aggregate, direct))

    def test_related_topic_and_follow_up_are_not_asserted_same_event(self):
        seizure = item("Tigray forces seize airport in northern Ethiopia", "BBC")
        reopened = item("Tigray officials reopen airport in northern Ethiopia", "Google News")
        other_action = item("Ethiopia and Tigray hold talks about northern airport", "Google News")
        self.assertFalse(same_event(seizure, reopened))
        self.assertFalse(same_event(seizure, other_action))

    def test_distant_or_same_source_or_attention_are_not_clustered(self):
        first = item("Tigray forces seize airport in northern Ethiopia", "BBC")
        headline = "Ethiopia: Tigray fighters capture airport in northern region"
        self.assertFalse(same_event(first, item(headline, "BBC")))
        self.assertFalse(same_event(first, item(headline, "Google News", hours=30)))
        self.assertFalse(same_event(first, item(headline, "Google Trends", source_type="google_trends")))


if __name__ == "__main__":
    unittest.main()
