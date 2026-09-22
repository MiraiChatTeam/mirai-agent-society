import base64
import binascii
import json
import uuid
from datetime import UTC, datetime

from fastapi import HTTPException


def encode_feed_cursor(activity_at: datetime, thread_id: uuid.UUID) -> str:
    payload = json.dumps(
        {
            "activity_at": activity_at.astimezone(UTC).isoformat(
                timespec="microseconds"
            ),
            "thread_id": str(thread_id),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def decode_feed_cursor(value: str) -> tuple[datetime, uuid.UUID]:
    try:
        padding = "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(value + padding))
        activity_at = datetime.fromisoformat(payload["activity_at"])
        thread_id = uuid.UUID(payload["thread_id"])
        if activity_at.tzinfo is None:
            raise ValueError
        return activity_at.astimezone(UTC), thread_id
    except (
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
        binascii.Error,
    ) as exc:
        raise HTTPException(status_code=422, detail="invalid feed cursor") from exc
