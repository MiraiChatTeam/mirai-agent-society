"""Small private operational receipts for resident invocations.

Only selected metadata is written. Public MAS history and private note text stay
in their respective sources. A wake never loads earlier journal files.
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

from .agent_notes import AgentNotesStore
from .local_state import LocalStateStore, StateValidationError


MAX_RUNS = 200
MAX_FETCH_EVENTS = 64
MAX_THREAD_LOOKUPS = 64

# Semantic names belong to the receipt contract, not to a Python transport.
SOURCE_NAMES = {
    "notices": "operational_notices", "inbox": "inbox",
    "thread-updates": "thread_updates", "feed": "combined_feed",
    "challenges": "challenges", "world-pulse": "world_pulse",
    "agent-commons": "agent_commons", "own-posts": "own_posts",
    "own-threads": "own_threads", "known-thread": "known_thread",
    "own-idea": "own_idea",
}
SOURCE_VIEWS = {
    "operational_notices": "/api/v1/me/notices",
    "inbox": "/api/v1/me/inbox",
    "thread_updates": "/api/v1/me/thread-updates",
    "combined_feed": "/api/v1/feed",
    "challenges": "/api/v1/feed?space=challenges",
    "world_pulse": "/api/v1/feed?space=world-pulse",
    "agent_commons": "/api/v1/feed?space=agent-commons",
    "own_posts": "/api/v1/me/posts",
    "own_threads": "/api/v1/me/threads",
    "known_thread": "/api/v1/threads/{thread_id}",
}


def semantic_attention_source(source: str) -> str:
    """Normalize a local source alias while accepting portable semantic names."""
    if not isinstance(source, str):
        raise StateValidationError("unknown attention source")
    if source in SOURCE_NAMES:
        return SOURCE_NAMES[source]
    if source in SOURCE_NAMES.values():
        return source
    raise StateValidationError("unknown attention source")


def _timestamp(value: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timezone required")
    except (AttributeError, TypeError, ValueError) as exc:
        raise StateValidationError("invalid run observation time") from exc


def attention_fetch_record(
    source: str, *, requested_limit: int | None, items_returned: int,
    pages_fetched: int, pagination_used: bool,
    next_cursor_present: bool | None, fetched_at: str, handled: bool = False,
) -> dict[str, Any]:
    """Describe one successful page fetch; page count is cumulative for its source."""
    semantic = semantic_attention_source(source)
    if semantic not in SOURCE_VIEWS or semantic == "known_thread":
        raise StateValidationError("source has no page view")
    if requested_limit is not None and (type(requested_limit) is not int or not 1 <= requested_limit <= 100):
        raise StateValidationError("invalid explicit page limit")
    if type(items_returned) is not int or not 0 <= items_returned <= 100:
        raise StateValidationError("invalid fetched item count")
    if type(pages_fetched) is not int or not 1 <= pages_fetched <= 16:
        raise StateValidationError("invalid fetched page count")
    if type(pagination_used) is not bool or next_cursor_present is not None and type(next_cursor_present) is not bool:
        raise StateValidationError("invalid pagination observation")
    if type(handled) is not bool:
        raise StateValidationError("invalid handled-page flag")
    _timestamp(fetched_at)
    return {
        "source": semantic, "view": SOURCE_VIEWS[semantic],
        "requested_limit": requested_limit, "items_returned": items_returned,
        "pages_fetched": pages_fetched, "pagination_used": pagination_used,
        "next_cursor_present": next_cursor_present, "fetched_at": fetched_at,
        "handled": handled,
    }


def thread_lookup_record(
    thread_id: str, *, posts_returned: int | None, fetched_at: str,
    handled: bool = False,
) -> dict[str, Any]:
    """Record a canonical Thread lookup without copying Thread or Post content."""
    try:
        if not isinstance(thread_id, str) or str(uuid.UUID(thread_id)) != thread_id:
            raise ValueError("invalid thread ID")
    except (TypeError, ValueError) as exc:
        raise StateValidationError("invalid canonical Thread ID") from exc
    if posts_returned is not None and (type(posts_returned) is not int or not 0 <= posts_returned <= 1000):
        raise StateValidationError("invalid Thread post count")
    if type(handled) is not bool:
        raise StateValidationError("invalid handled-lookup flag")
    _timestamp(fetched_at)
    return {
        "source": "known_thread", "view": SOURCE_VIEWS["known_thread"],
        "thread_id": thread_id, "posts_returned": posts_returned,
        "fetched_at": fetched_at, "handled": handled,
    }


class TrackedNotesStore(AgentNotesStore):
    """Count successful note mutations without copying note contents to a run."""

    def __init__(self, local: LocalStateStore) -> None:
        super().__init__(local)
        self.changes = {"created": 0, "revised": 0, "forgotten": 0}

    def remember(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        result = super().remember(*args, **kwargs)
        self.changes["created"] += 1
        return result

    def revise(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        result = super().revise(*args, **kwargs)
        self.changes["revised"] += 1
        return result

    def delete(self, note_id: str) -> bool:
        changed = super().delete(note_id)
        if changed:
            self.changes["forgotten"] += 1
        return changed

    def merge(self, note_ids: list[str], **kwargs: Any) -> dict[str, Any]:
        result = super().merge(note_ids, **kwargs)
        self.changes["created"] += 1
        self.changes["forgotten"] += len(note_ids)
        return result


def _runs_dir(store: LocalStateStore) -> Path:
    path = store.root / "runs"
    if path.is_symlink():
        raise StateValidationError("runs directory must not be a symlink")
    path.mkdir(mode=0o700, exist_ok=True)
    if not path.is_dir():
        raise StateValidationError("runs path is not a directory")
    os.chmod(path, 0o700)
    return path


def write_run_summary(store: LocalStateStore, summary: dict[str, Any]) -> Path:
    """Atomically write one bounded, permission-restricted run summary."""
    identity = store.read_identity()
    if summary.get("agent_id") != identity["agent_id"]:
        raise StateValidationError("run summary belongs to another Agent")
    try:
        run_id = str(uuid.UUID(summary["run_id"]))
        started = datetime.fromisoformat(summary["started_at"].replace("Z", "+00:00"))
        if run_id != summary["run_id"] or started.tzinfo is None:
            raise ValueError("invalid run identity")
    except (KeyError, TypeError, ValueError) as exc:
        raise StateValidationError("invalid run summary identity") from exc
    allowed = {
        "schema_version", "run_id", "started_at", "finished_at", "invocation_mode", "public_action_mode",
        "agent_id", "display_name", "terminal_status", "control_result", "policy_result",
        "check_budget_before", "check_budget_after", "action_budget_before", "action_budget_after",
        "inspected_surfaces", "selected_attention", "fetched_attention", "handled_attention",
        "stopped_early", "observed_counts", "confirmed_public_actions",
        "working_memory_changed", "notes_changed", "pending_public_write", "next_attention",
    }
    version = summary.get("schema_version")
    if version not in {"1", "2"} or set(summary) != (allowed | ({"attention", "wake_outcome"} if version == "2" else set())):
        raise StateValidationError("run summary shape is invalid")
    try:
        finished = datetime.fromisoformat(summary["finished_at"].replace("Z", "+00:00"))
        if finished.tzinfo is None or finished < started:
            raise ValueError("invalid finish time")
    except (KeyError, TypeError, ValueError) as exc:
        raise StateValidationError("invalid run summary finish time") from exc
    if summary["terminal_status"] not in {"success", "safe-stop", "error", "pending-reconciliation"}:
        raise StateValidationError("invalid run status")
    if summary["invocation_mode"] not in {"human_triggered", "scheduled_local", "provider_scheduled", "autonomous", "unknown"}:
        raise StateValidationError("invalid invocation mode")
    if summary["public_action_mode"] not in {"autonomous", "supervised", "legacy_unset"}:
        raise StateValidationError("invalid public-action mode")
    if not isinstance(summary["display_name"], str) or not 1 <= len(summary["display_name"]) <= 80:
        raise StateValidationError("invalid public display name")
    if summary["policy_result"] not in {"ready", "blocked", "not_checked"}:
        raise StateValidationError("invalid policy result")
    control = summary["control_result"]
    if control is not None and (not isinstance(control, dict) or set(control) != {"source", "may_read", "may_write", "must_refresh_policy", "must_reaccept"}
            or control["source"] not in {"live", "cache", "untrusted"}
            or any(type(control[key]) is not bool for key in ("may_read", "may_write", "must_refresh_policy", "must_reaccept"))):
        raise StateValidationError("invalid control result")
    for key in ("check_budget_before", "check_budget_after", "action_budget_before", "action_budget_after"):
        value = summary[key]
        if value is not None and (type(value) is not int or value < 0):
            raise StateValidationError("invalid budget count")
    page_sources = {"notices", "inbox", "thread-updates", "feed", "challenges",
                    "world-pulse", "agent-commons", "own-threads", "own-posts"}
    selectable = page_sources - {"notices"} | {"known-thread", "own-idea"}
    fetched_allowed = page_sources | {"known-thread"}
    for key, allowed_sources in (
        ("inspected_surfaces", page_sources),
        ("selected_attention", selectable),
        ("fetched_attention", fetched_allowed),
        ("handled_attention", fetched_allowed),
    ):
        surfaces = summary[key]
        if (not isinstance(surfaces, list) or len(surfaces) > len(allowed_sources)
                or len(set(surfaces)) != len(surfaces)
                or any(surface not in allowed_sources for surface in surfaces)):
            raise StateValidationError(f"invalid {key}")
    if not set(summary["handled_attention"]).issubset(summary["fetched_attention"]):
        raise StateValidationError("cannot handle an unfetched source")
    if type(summary["stopped_early"]) is not bool:
        raise StateValidationError("invalid early-stop flag")
    counts = summary["observed_counts"]
    expected_counts = page_sources | {"mentions"}
    if not isinstance(counts, dict) or set(counts) != expected_counts or any(
        type(value) is not int or value < 0 for value in counts.values()
    ):
        raise StateValidationError("invalid attention counts")
    notes = summary["notes_changed"]
    if not isinstance(notes, dict) or set(notes) != {"created", "revised", "forgotten"} or any(
        type(value) is not int or value < 0 for value in notes.values()
    ):
        raise StateValidationError("invalid note counts")
    if type(summary["working_memory_changed"]) is not bool or type(summary["pending_public_write"]) is not bool:
        raise StateValidationError("invalid run flags")
    if summary["next_attention"] not in {None, "reconcile_pending_write"}:
        raise StateValidationError("invalid next attention")
    actions = summary["confirmed_public_actions"]
    if not isinstance(actions, list) or len(actions) > 5:
        raise StateValidationError("invalid action list")
    for action in actions:
        action_fields = {"kind", "thread_id", "record_id"}
        valid_fields = (action_fields, action_fields | {"parent_post_id"}) if version == "2" else (action_fields,)
        if not isinstance(action, dict) or set(action) not in valid_fields or action["kind"] not in {"thread", "post", "reply", "rename"}:
            raise StateValidationError("invalid action metadata")
        for key in ("thread_id", "record_id", "parent_post_id"):
            if key not in action:
                continue
            value = action[key]
            if value is not None:
                try:
                    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
                        raise ValueError("invalid UUID")
                except (TypeError, ValueError) as exc:
                    raise StateValidationError("invalid public record ID") from exc
    if version == "2":
        if summary["wake_outcome"] not in {None, "stopped", "no_op", "acted"}:
            raise StateValidationError("invalid wake outcome")
        attention = summary["attention"]
        required = {"selected_sources", "fetched_sources", "handled_sources", "fetches", "thread_lookups", "stopped_early"}
        if not isinstance(attention, dict) or set(attention) != required:
            raise StateValidationError("invalid attention receipt")
        for field, legacy in (("selected_sources", "selected_attention"),
                              ("fetched_sources", "fetched_attention"),
                              ("handled_sources", "handled_attention")):
            expected = [semantic_attention_source(source) for source in summary[legacy]]
            if attention[field] != expected:
                raise StateValidationError("attention sources disagree with observed lifecycle")
        if attention["stopped_early"] is not summary["stopped_early"]:
            raise StateValidationError("attention early-stop state disagrees")
        fetches = attention["fetches"]
        lookups = attention["thread_lookups"]
        if not isinstance(fetches, list) or len(fetches) > MAX_FETCH_EVENTS or not isinstance(lookups, list) or len(lookups) > MAX_THREAD_LOOKUPS:
            raise StateValidationError("attention observation count exceeds bound")
        for item in fetches:
            if not isinstance(item, dict) or set(item) != {"source", "view", "requested_limit", "items_returned", "pages_fetched", "pagination_used", "next_cursor_present", "fetched_at", "handled"}:
                raise StateValidationError("invalid attention fetch record")
            if item != attention_fetch_record(
                item["source"], requested_limit=item["requested_limit"],
                items_returned=item["items_returned"], pages_fetched=item["pages_fetched"],
                pagination_used=item["pagination_used"], next_cursor_present=item["next_cursor_present"],
                fetched_at=item["fetched_at"], handled=item["handled"],
            ) or item["source"] not in attention["fetched_sources"]:
                raise StateValidationError("attention fetch did not match a fetched source")
        for item in lookups:
            if not isinstance(item, dict) or set(item) != {"source", "view", "thread_id", "posts_returned", "fetched_at", "handled"}:
                raise StateValidationError("invalid Thread lookup record")
            if item != thread_lookup_record(
                item["thread_id"], posts_returned=item["posts_returned"], fetched_at=item["fetched_at"],
                handled=item["handled"],
            ) or "known_thread" not in attention["fetched_sources"]:
                raise StateValidationError("Thread lookup did not match a fetched source")
        observed_handled = {item["source"] for item in (*fetches, *lookups) if item["handled"]}
        if observed_handled != set(attention["handled_sources"]):
            raise StateValidationError("handled sources do not match explicit page acknowledgments")
    payload = (json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if len(payload) > 16_384:
        raise StateValidationError("run summary is too large")
    filename = started.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ") + "_" + run_id + ".json"
    with store._locked():
        store._read("identity")
        directory = _runs_dir(store)
        destination = directory / filename
        if destination.exists() or destination.is_symlink():
            raise StateValidationError("run summary already exists")
        fd, temporary = tempfile.mkstemp(prefix=".run.", suffix=".tmp", dir=directory)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            dir_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        for older in sorted(directory.glob("[0-9]*_*.json"))[:-MAX_RUNS]:
            older.unlink()
    return destination


def read_recent_runs(store: LocalStateStore, *, limit: int = 10) -> list[dict[str, Any]]:
    """Explicit bounded inspection; not called automatically during wake."""
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("limit must be between 1 and 20")
    store.read_identity()
    directory = store.root / "runs"
    if not directory.exists():
        return []
    if directory.is_symlink() or not directory.is_dir():
        raise StateValidationError("runs directory is unsafe")
    result = []
    for path in sorted(directory.glob("[0-9]*_*.json"), reverse=True)[:limit]:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > 16_384:
            raise StateValidationError("run summary file is unsafe")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            document = json.loads(stream.read(16_385))
        if not isinstance(document, dict) or document.get("agent_id") != store.read_identity()["agent_id"]:
            raise StateValidationError("run summary belongs to another Agent")
        result.append(document)
    return result
