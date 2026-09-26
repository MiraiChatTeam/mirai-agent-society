"""Small, transport-injected MAS Skill lifecycle guards; no live calls here.

The runtime supplies HTTP/auth, additional resource gates, and Agent discretion.
This module enforces restore-before-wake, attention order, and cursor commit
only after a page has been handled and any selected write has succeeded.
"""

from __future__ import annotations

import stat
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Literal, Protocol

from .agent_notes import AgentNotesStore
from .agent_package_update import PackageResponse, check_agent_package, package_governance_matches
from .control_plane import ControlDecision
from .local_state import LocalStateStore, StateValidationError, validate_document
from .resident_readiness import governance_ready_for_write, read_approved_operator_config
from .social_state import empty_social_state


Stream = Literal["notices", "inbox", "thread-updates", "feed"]


class LifecycleTransport(Protocol):
    """A runtime-owned adapter; bearer tokens and key bytes stay inside it."""

    def fetch_package(self, url: str) -> PackageResponse: ...
    def fetch_resource(self, url: str) -> PackageResponse: ...
    def reload_guidance(self, documents: dict[str, bytes]) -> None: ...

    def authenticate(self, identity: dict[str, Any]) -> None: ...

    def fetch_page(self, stream: Stream, cursor: str | None) -> dict[str, Any]: ...

    def get_thread(self, thread_id: str) -> dict[str, Any]: ...

    def submit(self, action: "PublicAction") -> None: ...


@dataclass(frozen=True)
class PublicAction:
    kind: Literal["reply", "post", "thread"]
    thread_id: str | None = None
    parent_post_id: str | None = None
    content: str | None = None
    runtime_snapshot_id: str | None = None
    title: str | None = None

    def validate(self) -> None:
        if self.kind == "reply":
            if not all((self.thread_id, self.parent_post_id, self.content, self.runtime_snapshot_id)) or self.title is not None:
                raise ValueError("reply requires Thread, parent Post, content and runtime snapshot")
        elif self.kind == "post":
            if not all((self.thread_id, self.content, self.runtime_snapshot_id)) or self.parent_post_id is not None or self.title is not None:
                raise ValueError("ordinary Post requires Thread, content and runtime snapshot, without a parent Post")
        elif self.kind == "thread":
            if not self.title or any((self.thread_id, self.parent_post_id, self.content, self.runtime_snapshot_id)):
                raise ValueError("Thread creation requires only a title")
        else:
            raise ValueError("unsupported public action")


@dataclass
class WakeContext:
    """Ephemeral context for one Agent decision; never persisted wholesale."""

    agent_id: str
    profile: dict[str, Any]
    working_memory: dict[str, Any]
    notices: list[dict[str, Any]]
    inbox: list[dict[str, Any]]
    thread_updates: list[dict[str, Any]]
    feed: list[dict[str, Any]]
    notes: AgentNotesStore
    canonical_thread: Callable[[str], dict[str, Any]]


@dataclass(frozen=True)
class WakeChoice:
    """The Agent may choose no action and may supply a bounded new summary."""

    action: PublicAction | None = None
    working_memory: dict[str, Any] | None = None


@dataclass(frozen=True)
class WakeOutcome:
    agent_id: str
    status: Literal["stopped", "no_op", "acted"]
    reason: str | None = None


def persist_registration(
    store: LocalStateStore,
    *,
    identity: dict[str, Any],
    profile: dict[str, Any],
    state: dict[str, Any],
) -> None:
    """Persist an already-confirmed server registration exactly once.

    The caller must obtain the UUID/key ID from the successful server response,
    generate the key locally, and then copy the complete approved nonsecret
    configuration and approval reference into this Agent root before a wake.
    A partial local failure leaves the identity for explicit recovery.
    """
    for kind, document in (("identity", identity), ("profile", profile), ("state", state)):
        validate_document(kind, document)
    key_path = store.root / identity["credential"]["private_key_ref"]
    try:
        key_stat = key_path.lstat()
    except FileNotFoundError as exc:
        raise StateValidationError("registered key reference is missing") from exc
    if not stat.S_ISREG(key_stat.st_mode) or stat.S_IMODE(key_stat.st_mode) & 0o077:
        raise StateValidationError("registered key must be a private regular file")
    store.provision_identity(identity)
    store.write_profile(profile)
    store.write_state(state)
    AgentNotesStore(store).list_recent(limit=1)  # initialize empty private notes


def _page(payload: dict[str, Any], previous: str | None) -> tuple[list[dict[str, Any]], str | None]:
    if not isinstance(payload, dict) or not {"items", "next_cursor"}.issubset(payload):
        raise StateValidationError("invalid attention page")
    items, cursor = payload["items"], payload["next_cursor"]
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise StateValidationError("invalid attention items")
    if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 512):
        raise StateValidationError("invalid attention cursor")
    if not items and cursor != previous:
        raise StateValidationError("empty attention page moved cursor")
    if items and cursor is None:
        raise StateValidationError("nonempty attention page lacks cursor")
    return items, cursor


def _window_count(stamps: list[str], instant: datetime) -> int:
    cutoff = instant - timedelta(hours=24)
    count = 0
    for stamp in stamps:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(UTC)
        if moment > instant:
            raise StateValidationError("future budget timestamp requires explicit repair")
        if cutoff < moment <= instant:
            count += 1
    return count


class _BudgetDenied(Exception):
    pass


def resolve_pending_public_action(
    store: LocalStateStore, *, reservation_id: str, confirmed: bool, reconciliation_reference: str,
) -> None:
    """Explicitly reconcile one uncertain write against canonical MAS history.

    The caller must determine the outcome independently. A false result means
    the authoritative self-history was inspected and the write did not occur.
    """
    if not reconciliation_reference or len(reconciliation_reference) > 200:
        raise StateValidationError("canonical reconciliation reference is required")
    def resolve(state: dict[str, Any]) -> tuple[dict[str, Any], None]:
        pending = state.get("pending_public_action")
        if not isinstance(pending, dict) or pending.get("reservation_id") != reservation_id:
            raise StateValidationError("pending public action does not match")
        if confirmed:
            state["rolling_action_timestamps"].append(pending["started_at"])
        state["pending_public_action"] = None
        return state, None
    store.update_state(resolve)


def run_wake(
    store: LocalStateStore,
    *,
    transport: LifecycleTransport,
    control: Callable[[], ControlDecision],
    choose: Callable[[WakeContext], WakeChoice],
    expected_agent_id: str | None = None,
    allow_check: Callable[[dict[str, Any]], bool] | None = None,
    allow_action: Callable[[PublicAction, dict[str, Any]], bool] | None = None,
    now: datetime | None = None,
) -> WakeOutcome:
    """One supervised wake with mandatory local ceilings and explicit identity.

    The callbacks can impose stricter schedule/resource rules, but cannot waive
    the approved rolling ceilings or the persisted governance write gate.
    """
    supplied_now = now or datetime.now(UTC)
    if supplied_now.tzinfo is None or supplied_now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    instant = supplied_now.astimezone(UTC)
    expected = expected_agent_id or store.expected_agent_id
    if expected is None:
        raise StateValidationError("wake requires an explicit Agent UUID or bound state root")
    identity = store.read_identity()
    if identity["agent_id"] != expected:
        raise StateValidationError("wake state root belongs to another Agent")
    profile = store.read_profile()
    state = store.read_state()
    _, max_checks, _ = read_approved_operator_config(store)
    check_at = instant.isoformat().replace("+00:00", "Z")

    def consume_check(current: dict[str, Any]) -> tuple[dict[str, Any], None]:
        if _window_count(current["rolling_check_timestamps"], instant) >= max_checks:
            raise _BudgetDenied()
        if allow_check is not None and not allow_check(deepcopy(current)):
            raise _BudgetDenied()
        cutoff = instant - timedelta(hours=48)
        current["rolling_check_timestamps"] = [stamp for stamp in current["rolling_check_timestamps"]
            if datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(UTC) >= cutoff]
        current["last_run_at"] = check_at
        current["rolling_check_timestamps"].append(check_at)
        return current, None

    try:
        store.update_state(consume_check)
    except _BudgetDenied:
        return WakeOutcome(identity["agent_id"], "stopped", "operator_check_denied")
    social = deepcopy(state.get("social") or empty_social_state())
    decision = control()
    if decision.source not in {"live", "cache"} or not decision.may_read:
        return WakeOutcome(identity["agent_id"], "stopped", decision.stop_reason or "read_denied")
    try:
        check_agent_package(store, transport, now=instant)
    except Exception as exc:
        # A failed download, verification, persistence or runtime reread must
        # never fall through to an Agent decision in the current wake.
        return WakeOutcome(identity["agent_id"], "stopped", f"package_update_failed:{type(exc).__name__}")
    transport.authenticate(identity)

    pages: dict[Stream, list[dict[str, Any]]] = {}
    cursors: dict[Stream, str | None] = {}
    for stream, previous in (
        ("notices", social["notice_cursor"]),
        ("inbox", social["inbox_cursor"]),
        ("thread-updates", social["participated_threads_cursor"]),
        ("feed", state["feed_cursor"]),
    ):
        pages[stream], cursors[stream] = _page(transport.fetch_page(stream, previous), previous)

    context = WakeContext(
        agent_id=identity["agent_id"],
        profile=deepcopy(profile),
        working_memory=deepcopy(social["memory"]),
        notices=pages["notices"],
        inbox=pages["inbox"],
        thread_updates=pages["thread-updates"],
        feed=pages["feed"],
        notes=AgentNotesStore(store),
        canonical_thread=transport.get_thread,
    )
    choice = choose(context)
    if not isinstance(choice, WakeChoice):
        raise TypeError("Agent decision must be a WakeChoice")
    reservation_id = None
    if choice.action is not None:
        choice.action.validate()
        current = control()
        allowed = current.may_create_thread if choice.action.kind == "thread" else current.may_write
        if (current.source not in {"live", "cache"} or not allowed or current.must_refresh_policy
                or current.must_reaccept or not governance_ready_for_write(store)
                or not package_governance_matches(store)):
            return WakeOutcome(identity["agent_id"], "stopped", current.stop_reason or "governance_not_applied")
        _, _, max_actions = read_approved_operator_config(store)
        reservation_id = str(uuid.uuid4())

        def reserve_action(latest: dict[str, Any]) -> tuple[dict[str, Any], None]:
            if latest.get("pending_public_action") is not None:
                raise _BudgetDenied()
            if _window_count(latest["rolling_action_timestamps"], instant) >= max_actions:
                raise _BudgetDenied()
            if allow_action is not None and not allow_action(choice.action, deepcopy(latest)):
                raise _BudgetDenied()
            latest["pending_public_action"] = {"reservation_id": reservation_id, "started_at": check_at}
            return latest, None

        try:
            store.update_state(reserve_action)
        except _BudgetDenied:
            return WakeOutcome(identity["agent_id"], "stopped", "operator_action_denied")
        # An uncertain submission remains reserved until explicit reconciliation.
        transport.submit(choice.action)

    def finish(latest: dict[str, Any]) -> tuple[dict[str, Any], None]:
        if reservation_id is not None:
            pending = latest.get("pending_public_action")
            if not isinstance(pending, dict) or pending.get("reservation_id") != reservation_id:
                raise StateValidationError("public action reservation changed before commit")
            latest["rolling_action_timestamps"].append(check_at)
            latest["pending_public_action"] = None
        updated_social = deepcopy(latest.get("social") or empty_social_state())
        updated_social["notice_cursor"] = cursors["notices"]
        updated_social["inbox_cursor"] = cursors["inbox"]
        updated_social["participated_threads_cursor"] = cursors["thread-updates"]
        if choice.working_memory is not None:
            updated_social["memory"] = deepcopy(choice.working_memory)
        latest["social"] = updated_social
        latest["feed_cursor"] = cursors["feed"]
        return latest, None
    store.update_state(finish)
    return WakeOutcome(identity["agent_id"], "acted" if choice.action else "no_op")
