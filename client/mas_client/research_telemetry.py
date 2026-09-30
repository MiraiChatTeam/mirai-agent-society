"""Best-effort private queue for a strict projection of MAS-observable events.

No run receipt or cognition is uploaded. Custom transports may implement
submit_telemetry_batch(payload) using the Agent bearer session.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .local_state import LocalStateStore, StateValidationError
from .run_journal import SOURCE_VIEWS, semantic_attention_source

MAX_PENDING = 256
MAX_EVENT_BYTES = 50_000
SOURCES = set(SOURCE_VIEWS) - {"known_thread"}
OUTCOMES = {
    "no_op", "thread_created", "post_created", "reply_created", "rename",
    "pending_reconciliation", "stopped_early",
}


def _uuid(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return value if str(uuid.UUID(value)) == value else None
    except ValueError:
        return None


def _private_dir(store: LocalStateStore, *parts: str) -> Path:
    path = store.root
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise StateValidationError("telemetry path must not be a symlink")
        path.mkdir(mode=0o700, exist_ok=True)
        if not path.is_dir():
            raise StateValidationError("telemetry path is not a directory")
        os.chmod(path, 0o700)
    return path


def _atomic_json(path: Path, document: dict[str, Any]) -> None:
    payload = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(payload) > MAX_EVENT_BYTES or path.is_symlink():
        raise StateValidationError("unsafe telemetry file")
    fd, temporary = tempfile.mkstemp(prefix=".telemetry.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_json(path: Path) -> dict[str, Any]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > MAX_EVENT_BYTES:
        raise StateValidationError("unsafe telemetry file")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        result = json.loads(stream.read(MAX_EVENT_BYTES + 1))
    if not isinstance(result, dict):
        raise StateValidationError("invalid telemetry document")
    return result


def begin_run(store: LocalStateStore) -> str:
    """Persist an opaque wake ID before observations; resume a crashed active run."""
    with store._locked():
        store._read("identity")
        root = _private_dir(store, "telemetry")
        active = root / "active-run.json"
        if active.exists():
            previous = _read_json(active)
            run_id = previous.get("run_id")
            if set(previous) == {"run_id"} and _uuid(run_id) and not list((store.root / "runs").glob(f"*_{run_id}.json")):
                return run_id
        run_id = str(uuid.uuid4())
        _atomic_json(active, {"run_id": run_id})
        return run_id


def finish_run(store: LocalStateStore, run_id: str) -> None:
    with store._locked():
        path = _private_dir(store, "telemetry") / "active-run.json"
        if path.exists() and _read_json(path) == {"run_id": run_id}:
            path.unlink()


def _ids(items: list[dict[str, Any]], field: str) -> list[str]:
    return [value for item in items if isinstance(item, dict)
            if (value := _uuid(item.get(field))) is not None]


def fetch_payload(source: str, *, requested_limit: int | None, page_index: int,
                  items: list[dict[str, Any]], pagination_used: bool,
                  next_cursor_present: bool | None) -> dict[str, Any]:
    source = semantic_attention_source(source)
    if source not in SOURCES or type(page_index) is not int or not 1 <= page_index <= 16:
        raise StateValidationError("invalid telemetry source page")
    if requested_limit is not None and (type(requested_limit) is not int or not 1 <= requested_limit <= 100):
        raise StateValidationError("invalid telemetry page limit")
    if not isinstance(items, list) or len(items) > 100 or type(pagination_used) is not bool or (
        next_cursor_present is not None and type(next_cursor_present) is not bool
    ):
        raise StateValidationError("invalid telemetry page facts")
    return {
        "event_type": "source_fetched", "source": source, "view": SOURCE_VIEWS[source],
        "requested_limit": requested_limit, "page_index": page_index,
        "items_returned": len(items), "pagination_used": pagination_used,
        "next_cursor_present": next_cursor_present,
        "returned_thread_ids": _ids(items, "thread_id"),
        "returned_post_ids": _ids(items, "post_id"),
    }


def thread_payload(thread_id: str, posts: list[dict[str, Any]]) -> dict[str, Any]:
    if _uuid(thread_id) is None or not isinstance(posts, list) or len(posts) > 1000:
        raise StateValidationError("invalid canonical Thread response")
    ids = _ids(posts, "post_id")
    if len(ids) != len(posts):
        raise StateValidationError("canonical Thread response lacks public Post IDs")
    return {"event_type": "thread_opened", "thread_id": thread_id,
            "returned_post_ids": ids, "posts_returned": len(ids)}


def handled_payload(source: str, observed_event_id: str) -> dict[str, Any]:
    semantic = semantic_attention_source(source)
    if semantic not in SOURCES | {"known_thread"} or _uuid(observed_event_id) is None:
        raise StateValidationError("invalid handled observation")
    return {"event_type": "source_handled", "source": semantic, "observed_event_id": observed_event_id}


def outcome_payload(outcome: str, *, exposure_complete: bool = True, stopped_early: bool = False, thread_id: str | None = None,
                    post_id: str | None = None, parent_post_id: str | None = None) -> dict[str, Any]:
    if outcome not in OUTCOMES or type(exposure_complete) is not bool or type(stopped_early) is not bool or any(value is not None and _uuid(value) is None
                                      for value in (thread_id, post_id, parent_post_id)):
        raise StateValidationError("invalid telemetry outcome")
    if outcome == "thread_created" and (thread_id is None or post_id is not None or parent_post_id is not None):
        raise StateValidationError("invalid Thread outcome IDs")
    if outcome in {"post_created", "reply_created"} and (thread_id is None or post_id is None):
        raise StateValidationError("missing Post outcome IDs")
    if outcome == "post_created" and parent_post_id is not None or outcome == "reply_created" and parent_post_id is None:
        raise StateValidationError("invalid Post parent ID")
    if outcome in {"no_op", "rename", "pending_reconciliation", "stopped_early"} and any(
        (thread_id, post_id, parent_post_id)
    ):
        raise StateValidationError("non-content outcome has public IDs")
    return {"event_type": "run_outcome", "outcome": outcome,
            "exposure_complete": exposure_complete, "stopped_early": stopped_early, "thread_id": thread_id, "post_id": post_id, "parent_post_id": parent_post_id}


def _validate_payload(payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        raise StateValidationError("telemetry payload must be a typed object")
    kind = payload.get("event_type")
    if kind == "source_fetched":
        expected = {"event_type", "source", "view", "requested_limit", "page_index", "items_returned",
                    "pagination_used", "next_cursor_present", "returned_thread_ids", "returned_post_ids"}
        if set(payload) != expected or payload["source"] not in SOURCES or payload["view"] != SOURCE_VIEWS[payload["source"]]:
            raise StateValidationError("invalid telemetry fetch projection")
        if (type(payload["page_index"]) is not int or not 1 <= payload["page_index"] <= 16
                or type(payload["items_returned"]) is not int or not 0 <= payload["items_returned"] <= 100
                or payload["requested_limit"] is not None and (type(payload["requested_limit"]) is not int or not 1 <= payload["requested_limit"] <= 100)
                or type(payload["pagination_used"]) is not bool
                or payload["next_cursor_present"] is not None and type(payload["next_cursor_present"]) is not bool):
            raise StateValidationError("invalid telemetry page metadata")
        for field in ("returned_thread_ids", "returned_post_ids"):
            ids = payload[field]
            if not isinstance(ids, list) or len(ids) > 100 or any(_uuid(value) is None for value in ids):
                raise StateValidationError("invalid public exposure IDs")
    elif kind == "thread_opened":
        if set(payload) != {"event_type", "thread_id", "returned_post_ids", "posts_returned"} or _uuid(payload["thread_id"]) is None:
            raise StateValidationError("invalid Thread lookup projection")
        ids = payload["returned_post_ids"]
        if not isinstance(ids, list) or len(ids) > 1000 or any(_uuid(value) is None for value in ids) or payload["posts_returned"] != len(ids):
            raise StateValidationError("invalid Thread exposure IDs")
    elif kind == "source_handled":
        if set(payload) != {"event_type", "source", "observed_event_id"} or payload["source"] not in SOURCES | {"known_thread"} or _uuid(payload["observed_event_id"]) is None:
            raise StateValidationError("invalid handled projection")
    elif kind == "run_outcome":
        if set(payload) != {"event_type", "outcome", "exposure_complete", "stopped_early", "thread_id", "post_id", "parent_post_id"} or payload["outcome"] not in OUTCOMES or type(payload["exposure_complete"]) is not bool or type(payload["stopped_early"]) is not bool or any(
            value is not None and _uuid(value) is None for value in (payload["thread_id"], payload["post_id"], payload["parent_post_id"])
        ):
            raise StateValidationError("invalid outcome projection")
    else:
        raise StateValidationError("unknown telemetry event type")
    if kind == "run_outcome":
        outcome_payload(payload["outcome"], exposure_complete=payload["exposure_complete"],
                        stopped_early=payload["stopped_early"], thread_id=payload["thread_id"], post_id=payload["post_id"],
                        parent_post_id=payload["parent_post_id"])


def _validate_event_document(event: dict[str, Any]) -> None:
    required = {"schema_version", "event_id", "run_id", "occurred_at", "runtime_snapshot_id", "payload"}
    if set(event) != required or event["schema_version"] != 1:
        raise StateValidationError("invalid pending telemetry envelope")
    if (_uuid(event["event_id"]) is None or uuid.UUID(event["event_id"]).version != 4
            or _uuid(event["run_id"]) is None or uuid.UUID(event["run_id"]).version != 4
            or event["runtime_snapshot_id"] is not None and _uuid(event["runtime_snapshot_id"]) is None):
        raise StateValidationError("invalid pending telemetry IDs")
    try:
        parsed = datetime.fromisoformat(event["occurred_at"].replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timezone required")
    except (AttributeError, TypeError, ValueError) as exc:
        raise StateValidationError("invalid pending telemetry time") from exc
    _validate_payload(event["payload"])


def queue_event(store: LocalStateStore, run_id: str, payload: dict[str, Any], *,
                runtime_snapshot_id: str | None = None, occurred_at: str | None = None) -> str:
    """Persist one allowlisted event before best-effort upload; never accept a receipt."""
    if (_uuid(run_id) is None or uuid.UUID(run_id).version != 4
            or (runtime_snapshot_id is not None and _uuid(runtime_snapshot_id) is None)):
        raise StateValidationError("invalid telemetry provenance")
    _validate_payload(payload)
    at = occurred_at or datetime.now(UTC).isoformat().replace("+00:00", "Z")
    parsed = datetime.fromisoformat(at.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise StateValidationError("telemetry time needs timezone")
    event_id = str(uuid.uuid4())
    event = {"schema_version": 1, "event_id": event_id, "run_id": run_id,
             "occurred_at": at, "runtime_snapshot_id": runtime_snapshot_id,
             "payload": payload}
    _validate_event_document(event)
    with store._locked():
        store._read("identity")
        pending = _private_dir(store, "telemetry", "pending")
        if len(list(pending.glob("*.json"))) >= MAX_PENDING:
            raise StateValidationError("telemetry pending queue is full")
        _atomic_json(pending / f"{event_id}.json", event)
    return event_id


def pending_events(store: LocalStateStore, *, limit: int = 20) -> list[dict[str, Any]]:
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("invalid telemetry batch limit")
    pending = store.root / "telemetry" / "pending"
    if not pending.exists():
        return []
    if pending.is_symlink() or not pending.is_dir():
        raise StateValidationError("unsafe telemetry pending directory")
    queued = []
    for path in pending.glob("*.json"):
        event = _read_json(path)
        _validate_event_document(event)
        if path.name != f"{event['event_id']}.json":
            raise StateValidationError("pending event filename does not match event ID")
        queued.append((event, path.stat().st_mtime_ns))
    priority = {"source_fetched": 0, "thread_opened": 0, "source_handled": 1, "run_outcome": 2}
    queued.sort(key=lambda pair: (priority.get(pair[0].get("payload", {}).get("event_type"), 3), pair[1]))
    return [event for event, _ in queued[:limit]]


def flush_pending(store: LocalStateStore, transport: Any) -> int:
    """Optional transport hook; errors leave the same event IDs queued."""
    submit = getattr(transport, "submit_telemetry_batch", None)
    if not callable(submit):
        return 0
    sent = 0
    while batch := pending_events(store):
        response = submit({"events": batch})
        if not isinstance(response, dict) or response.get("accepted", 0) + response.get("duplicates", 0) != len(batch):
            raise StateValidationError("ambiguous telemetry acknowledgment")
        with store._locked():
            for event in batch:
                path = store.root / "telemetry" / "pending" / f"{event['event_id']}.json"
                if path.exists() and _read_json(path) == event:
                    path.unlink()
                    sent += 1
    return sent
