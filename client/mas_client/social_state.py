"""Bounded, replaceable local orientation state; never canonical social truth."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .local_state import LocalStateStore, validate_document


def empty_social_state() -> dict[str, Any]:
    return {
        "inbox_cursor": None,
        "notice_cursor": None,
        "own_activity_cursor": None,
        "participated_threads_cursor": None,
        "memory": {
            "updated_at": None,
            "window_start": None,
            "summary": "",
            "active_threads": [],
        },
    }


def update_social_state(store: LocalStateStore, social: dict[str, Any]) -> None:
    """Atomically update only the social section after a completed run.

    Callers derive summaries themselves and must advance a cursor only after
    successfully handling that page. The helper neither fetches nor posts.
    """
    state = store.read_state()
    updated = deepcopy(state)
    updated["social"] = deepcopy(social)
    validate_document("state", updated)
    store.write_state(updated)
