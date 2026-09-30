"""Strict v1 request projection; no database or real resident involved."""

import unittest
import uuid
from datetime import UTC, datetime

from pydantic import ValidationError

from app.research_telemetry import TelemetryBatchIn, _digest


RUN_ID = str(uuid.uuid4())
EVENT_ID = str(uuid.uuid4())
THREAD_ID = str(uuid.uuid4())


def event(payload, **extra):
    return {
        "schema_version": 1, "run_id": RUN_ID, "event_id": EVENT_ID,
        "occurred_at": datetime.now(UTC).isoformat(), "runtime_snapshot_id": None,
        "payload": payload, **extra,
    }


def fetch_payload():
    return {
        "event_type": "source_fetched", "source": "combined_feed", "view": "/api/v1/feed",
        "requested_limit": None, "page_index": 1, "items_returned": 1,
        "pagination_used": False, "next_cursor_present": None,
        "returned_thread_ids": [THREAD_ID], "returned_post_ids": [],
    }


class TelemetrySchemaTests(unittest.TestCase):
    def test_observable_v1_event_is_stable(self):
        parsed = TelemetryBatchIn.model_validate({"events": [event(fetch_payload())]})
        self.assertEqual(parsed.events[0].payload.returned_thread_ids, [uuid.UUID(THREAD_ID)])
        self.assertEqual(_digest(parsed.events[0]), _digest(parsed.events[0]))

    def test_unknown_version_fields_and_cognition_are_rejected(self):
        for mutation in (
            lambda e: e.update(schema_version=2),
            lambda e: e.update(agent_id=str(uuid.uuid4())),
            lambda e: e["payload"].update(selected_sources=["inbox"]),
            lambda e: e["payload"].update(content="public Post text"),
            lambda e: e["payload"].update(prompt="private prompt"),
            lambda e: e["payload"].update(operator_identity="name"),
            lambda e: e["payload"].update(local_path="/home/operator"),
            lambda e: e["payload"].update(bearer_token="secret"),
            lambda e: e["payload"].update(private_key="secret"),
            lambda e: e["payload"].update(view="/api/v1/feed?token=secret"),
        ):
            with self.subTest(mutation=mutation):
                item = event(fetch_payload())
                mutation(item)
                with self.assertRaises(ValidationError):
                    TelemetryBatchIn.model_validate({"events": [item]})

    def test_batch_and_action_shapes_are_bounded(self):
        with self.assertRaises(ValidationError):
            TelemetryBatchIn.model_validate({"events": [event(fetch_payload())] * 21})
        with self.assertRaises(ValidationError):
            TelemetryBatchIn.model_validate({"events": [event({
                "event_type": "run_outcome", "outcome": "reply_created", "exposure_complete": True, "thread_id": THREAD_ID,
                "post_id": str(uuid.uuid4()), "parent_post_id": None,
            })]})
        parsed = TelemetryBatchIn.model_validate({"events": [event({
            "event_type": "run_outcome", "outcome": "no_op", "exposure_complete": True,
        })]})
        self.assertEqual(parsed.events[0].payload.outcome, "no_op")
        self.assertTrue(parsed.events[0].payload.exposure_complete)
        self.assertFalse(parsed.events[0].payload.stopped_early)
