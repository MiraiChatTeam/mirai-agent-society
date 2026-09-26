"""Agent-facing World Pulse text remains descriptive across new and legacy rows."""

import unittest
from datetime import UTC, datetime
from uuid import uuid4

from app.schemas import WorldPulseItemRead
from app.world_pulse_acquisition import NEUTRAL_PROMPT


class WorldPulseResidentTextTests(unittest.TestCase):
    def test_new_acquisition_description_is_not_an_instruction(self):
        self.assertEqual(NEUTRAL_PROMPT, "External source item.")

    def test_legacy_summary_is_masked_in_public_api_model(self):
        legacy = WorldPulseItemRead.model_validate({
            "pulse_id": uuid4(), "title": "A source headline",
            "summary": "Discuss this development.", "display_summary": "Discuss this development.",
            "stimulus_summary": None, "summary_source": "unavailable",
            "verification_status": "source_report_unverified", "language": "en",
            "published_at": datetime.now(UTC), "ingested_at": datetime.now(UTC),
            "source_type": "news", "source_url": "https://example.org/item",
            "source_name": "Example Source", "external_id": None, "cluster_key": None,
        })
        self.assertEqual(legacy.summary, legacy.title)
        self.assertEqual(legacy.display_summary, legacy.title)
