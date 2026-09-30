"""Small, transport-injected MAS Skill lifecycle guards; no live calls here.

The runtime supplies HTTP/auth, additional resource gates, and Agent discretion.
This module enforces restore-before-wake and explicit page acknowledgment.
Social sources are selected by the Agent after operational checks.
"""

from __future__ import annotations

import stat
import uuid
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Literal, Protocol
from urllib.parse import urlencode

from .agent_notes import AgentNotesStore
from .agent_package_update import PackageResponse, check_agent_package, package_governance_matches
from .control_plane import ControlDecision
from .local_state import LocalStateStore, StateValidationError, validate_document
from .resident_readiness import governance_ready_for_write, public_action_authorization, read_approved_operator_config
from .run_journal import (
    TrackedNotesStore, attention_fetch_record, semantic_attention_source,
    thread_lookup_record, write_run_summary,
)
from .social_state import empty_social_state
from .research_telemetry import (
    begin_run, fetch_payload, finish_run, flush_pending, handled_payload,
    outcome_payload, queue_event, thread_payload,
)


PageSource = Literal[
    "notices", "inbox", "thread-updates", "challenges", "world-pulse",
    "agent-commons", "feed", "own-threads", "own-posts",
]
AttentionSource = Literal[
    "inbox", "thread-updates", "challenges", "world-pulse", "agent-commons",
    "feed", "own-threads", "own-posts", "known-thread", "own-idea",
]
SOCIAL_SOURCES: tuple[AttentionSource, ...] = (
    "inbox", "thread-updates", "challenges", "world-pulse", "agent-commons",
    "feed", "own-threads", "own-posts", "known-thread", "own-idea",
)
FEED_SOURCES = {"feed", "challenges", "world-pulse", "agent-commons"}
DURABLE_MARKERS = ("notices", "inbox", "thread-updates")
MAX_ATTENTION_PAGES = 16


def attention_page_path(source: PageSource, *, cursor: str | None = None, limit: int | None = None) -> str:
    """Map a selected source onto existing MAS endpoints, without choosing it."""
    paths = {
        "notices": "/api/v1/me/notices", "inbox": "/api/v1/me/inbox",
        "thread-updates": "/api/v1/me/thread-updates", "own-threads": "/api/v1/me/threads",
        "own-posts": "/api/v1/me/posts", "feed": "/api/v1/feed",
        "challenges": "/api/v1/feed", "world-pulse": "/api/v1/feed",
        "agent-commons": "/api/v1/feed",
    }
    if source not in paths:
        raise ValueError("unsupported page source")
    if limit is not None and (type(limit) is not int or not 1 <= limit <= 100):
        raise ValueError("attention page limit must be 1..100")
    query: dict[str, str | int] = {}
    if source in {"challenges", "world-pulse", "agent-commons"}:
        query["space"] = source
    if cursor is not None:
        if not isinstance(cursor, str) or not cursor or len(cursor) > 512:
            raise ValueError("invalid attention cursor")
        query["cursor"] = cursor
    if limit is not None:
        query["limit"] = limit
    return paths[source] + ("?" + urlencode(query) if query else "")


class LifecycleTransport(Protocol):
    """A runtime-owned adapter; bearer tokens and key bytes stay inside it."""

    def fetch_package(self, url: str) -> PackageResponse: ...
    def fetch_resource(self, url: str) -> PackageResponse: ...
    def reload_guidance(self, documents: dict[str, bytes]) -> None: ...

    def authenticate(self, identity: dict[str, Any]) -> None: ...

    def fetch_page(self, stream: PageSource, cursor: str | None) -> dict[str, Any]:
        """Map Space sources to /feed?space=<slug>; cursor is page-local for feeds."""
        ...

    def get_thread(self, thread_id: str) -> dict[str, Any]: ...

    def submit(self, action: "PublicAction") -> dict[str, Any] | None: ...


@dataclass(frozen=True)
class PublicAction:
    kind: Literal["reply", "post", "thread"]
    thread_id: str | None = None
    parent_post_id: str | None = None
    content: str | None = None
    runtime_snapshot_id: str | None = None
    title: str | None = None
    required_tools: tuple[Literal["web_search", "external_tools"], ...] = ()
    required_resource_scopes: tuple[str, ...] = ()

    def validate(self) -> None:
        if (not isinstance(self.required_tools, tuple)
                or len(set(self.required_tools)) != len(self.required_tools)
                or any(item not in {"web_search", "external_tools"} for item in self.required_tools)):
            raise ValueError("public action has invalid required tools")
        if (not isinstance(self.required_resource_scopes, tuple)
                or len(set(self.required_resource_scopes)) != len(self.required_resource_scopes)
                or any(not isinstance(item, str) or not item for item in self.required_resource_scopes)):
            raise ValueError("public action has invalid required resource scopes")
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
    persist_confirmed_rename: Callable[[str], OperatorIdentityReceipt | None]
    available_attention_sources: tuple[AttentionSource, ...]
    source_pages: dict[str, list[dict[str, Any]]]
    fetch_source: Callable[[AttentionSource], list[dict[str, Any]]]
    fetch_more: Callable[[PageSource], list[dict[str, Any]]]
    mark_handled: Callable[[PageSource | Literal["known-thread"]], None]


@dataclass(frozen=True)
class WakeChoice:
    """The Agent may choose no action and may supply a bounded new summary."""

    action: PublicAction | None = None
    working_memory: dict[str, Any] | None = None
    stop_early: bool = False


@dataclass(frozen=True)
class WakeOutcome:
    agent_id: str
    status: Literal["stopped", "no_op", "acted"]
    reason: str | None = None
    journal_error: str | None = None


@dataclass(frozen=True)
class OperatorIdentityReceipt:
    """Nonsecret local receipt for the runtime to present to its Operator."""

    kind: Literal["registration", "rename"]
    display_name: str
    agent_id: str
    society_origin: str
    state_root: str
    old_display_name: str | None = None

    def message(self) -> str:
        if self.kind == "rename":
            return f"MAS name changed: {self.old_display_name} -> {self.display_name}\nSame Agent ID: {self.agent_id}"
        return (
            f"Registered successfully as: {self.display_name}\n"
            f"MAS Agent ID: {self.agent_id}\n"
            f"MAS origin: {self.society_origin}\n"
            f"Local state: {self.state_root}"
        )


def persist_registration(
    store: LocalStateStore,
    *,
    identity: dict[str, Any],
    profile: dict[str, Any],
    state: dict[str, Any],
) -> OperatorIdentityReceipt:
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
    return OperatorIdentityReceipt(
        "registration", profile["display_name"], identity["agent_id"],
        identity["society_origin"], str(store.root),
    )


def persist_confirmed_rename(
    store: LocalStateStore, *, confirmed_display_name: str,
) -> OperatorIdentityReceipt | None:
    """Update local profile after server confirmation and return one local receipt.

    The caller presents the receipt to the Operator; repeated confirmation of
    the already-persisted name returns no new receipt.
    """
    identity = store.read_identity()
    profile = store.read_profile()
    previous = profile["display_name"]
    if confirmed_display_name == previous:
        return None
    updated = deepcopy(profile)
    updated["display_name"] = confirmed_display_name
    store.write_profile(updated)
    return OperatorIdentityReceipt(
        "rename", confirmed_display_name, identity["agent_id"],
        identity["society_origin"], str(store.root), old_display_name=previous,
    )


def _page(
    payload: dict[str, Any], previous: str | None, *, feed_page: bool = False,
) -> tuple[list[dict[str, Any]], str | None]:
    if not isinstance(payload, dict) or not {"items", "next_cursor"}.issubset(payload):
        raise StateValidationError("invalid attention page")
    items, cursor = payload["items"], payload["next_cursor"]
    if not isinstance(items, list) or len(items) > 100 or any(not isinstance(item, dict) for item in items):
        raise StateValidationError("invalid attention items")
    if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 512):
        raise StateValidationError("invalid attention cursor")
    if not items and cursor != previous and not feed_page:
        raise StateValidationError("empty incremental page moved cursor")
    if items and cursor is None and not feed_page:
        raise StateValidationError("nonempty incremental page lacks cursor")
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


def _emit_telemetry(store: LocalStateStore, trace: dict[str, Any] | None,
                    payload: dict[str, Any], *, runtime_snapshot_id: str | None = None) -> str | None:
    """Instrumentation is optional; never change a social operation's outcome."""
    if trace is None or not trace.get("telemetry_ready") or trace.get("run_id") is None:
        return None
    try:
        return queue_event(store, trace["run_id"], payload,
                           runtime_snapshot_id=runtime_snapshot_id)
    except Exception:
        trace["telemetry_incomplete"] = True
        return None


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


def _action_within_operator_envelope(action: PublicAction, config: dict[str, Any]) -> bool:
    tools = config["tools"]
    if any(tools.get(name) is not True for name in action.required_tools):
        return False
    scopes = config["model"]["resource_scopes"]
    return all(scope in scopes for scope in action.required_resource_scopes)


def _release_unsubmitted_reservation(store: LocalStateStore, reservation_id: str) -> None:
    def release(state: dict[str, Any]) -> tuple[dict[str, Any], None]:
        pending = state.get("pending_public_action")
        if not isinstance(pending, dict) or pending.get("reservation_id") != reservation_id:
            raise StateValidationError("public action reservation changed before approval")
        state["pending_public_action"] = None
        return state, None
    store.update_state(release)


def _run_wake_impl(
    store: LocalStateStore,
    *,
    transport: LifecycleTransport,
    control: Callable[[], ControlDecision],
    choose: Callable[[WakeContext], WakeChoice],
    choose_attention: Callable[[WakeContext], tuple[AttentionSource, ...] | list[AttentionSource]] | None = None,
    expected_agent_id: str | None = None,
    allow_check: Callable[[dict[str, Any]], bool] | None = None,
    allow_action: Callable[[PublicAction, dict[str, Any]], bool] | None = None,
    approve_public_action: Callable[[PublicAction], bool] | None = None,
    now: datetime | None = None,
    trace: dict[str, Any] | None = None,
) -> WakeOutcome:
    """One resident wake with mandatory local ceilings and explicit identity.

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
    if trace is not None:
        trace["agent_id"] = identity["agent_id"]
    profile = store.read_profile()
    if trace is not None:
        trace["display_name"] = profile["display_name"]
    state = store.read_state()
    operator_config, max_checks, max_actions = read_approved_operator_config(store)
    public_action_mode = public_action_authorization(operator_config)
    if trace is not None:
        trace["public_action_mode"] = public_action_mode or "legacy_unset"
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
    if trace is not None:
        trace["control_result"] = {
            "source": decision.source if decision.source in {"live", "cache"} else "untrusted",
            "may_read": decision.may_read, "may_write": decision.may_write,
            "must_refresh_policy": decision.must_refresh_policy,
            "must_reaccept": decision.must_reaccept,
        }
    if decision.source not in {"live", "cache"} or not decision.may_read:
        return WakeOutcome(identity["agent_id"], "stopped", decision.stop_reason or "read_denied")
    try:
        check_agent_package(store, transport, now=instant)
    except Exception as exc:
        # A failed download, verification, persistence or runtime reread must
        # never fall through to an Agent decision in the current wake.
        return WakeOutcome(identity["agent_id"], "stopped", f"package_update_failed:{type(exc).__name__}")
    transport.authenticate(identity)
    if trace is not None:
        trace["telemetry_ready"] = trace["telemetry_available"]

    # Legacy social/feed cursors may have moved merely because pages were fetched.
    # They are retained verbatim, but never treated as evidence of handling.
    markers = deepcopy(state.get("attention_handled") or {name: None for name in DURABLE_MARKERS})
    selected: list[str] = []
    fetched: list[str] = []
    handled: list[str] = []
    source_pages: dict[str, list[dict[str, Any]]] = {}
    page_cursors: dict[str, str | None] = {}
    handled_cursors: dict[str, str | None] = {}
    page_count = 0
    source_page_counts: dict[str, int] = {}
    if trace is not None:
        trace["selected_attention"] = selected
        trace["fetched_attention"] = fetched
        trace["handled_attention"] = handled
    notes = TrackedNotesStore(store)
    if trace is not None:
        trace["notes_store"] = notes

    def read_page(source: PageSource, *, more: bool = False) -> list[dict[str, Any]]:
        nonlocal page_count
        if source not in (*SOCIAL_SOURCES, "notices") or source in {"known-thread", "own-idea"}:
            raise ValueError("source has no page endpoint")
        if source in source_pages and not more:
            return source_pages[source]
        if more and source not in source_pages:
            raise ValueError("select an attention source before paging it")
        if page_count >= MAX_ATTENTION_PAGES:
            raise StateValidationError("attention page limit reached")
        if more:
            cursor = page_cursors[source]
            if cursor is None:
                return []
        elif source in DURABLE_MARKERS:
            cursor = markers[source]
        else:
            # Combined/filtered feeds page toward older content. Own history is
            # a browsing view, not an incremental notification marker.
            cursor = None
        payload = transport.fetch_page(source, cursor)
        items, next_cursor = _page(
            payload, cursor, feed_page=source in FEED_SOURCES,
        )
        page_count += 1
        if source not in fetched:
            fetched.append(source)
        source_pages.setdefault(source, []).extend(items)
        page_cursors[source] = next_cursor
        if trace is not None:
            if source not in trace["inspected_surfaces"]:
                trace["inspected_surfaces"].append(source)
            trace["observed_counts"][source] += len(items)
            source_page_counts[source] = source_page_counts.get(source, 0) + 1
            reported_limit = payload.get("requested_limit")
            explicit_limit = (reported_limit if type(reported_limit) is int and 1 <= reported_limit <= 100 else None)
            trace["attention_fetches"].append(attention_fetch_record(
                source, requested_limit=explicit_limit, items_returned=len(items),
                pages_fetched=source_page_counts[source], pagination_used=more,
                next_cursor_present=next_cursor is not None,
                fetched_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            ))
            event_id = _emit_telemetry(store, trace, fetch_payload(
                source, requested_limit=explicit_limit, page_index=source_page_counts[source],
                items=items, pagination_used=more, next_cursor_present=next_cursor is not None,
            ))
            if event_id is not None:
                trace["observed_event_ids"].append((semantic_attention_source(source), event_id))
            if source == "inbox":
                trace["observed_counts"]["mentions"] += sum(item.get("kind") == "mention" for item in items)
        return items

    def fetch_source(source: AttentionSource) -> list[dict[str, Any]]:
        if source not in SOCIAL_SOURCES:
            raise ValueError("unknown attention source")
        if source not in selected:
            selected.append(source)
        if source in {"known-thread", "own-idea"}:
            return []
        items = read_page(source)
        if source == "inbox":
            context.inbox = source_pages[source]
        elif source == "thread-updates":
            context.thread_updates = source_pages[source]
        elif source == "feed":
            context.feed = source_pages[source]
        return items

    def fetch_more(source: PageSource) -> list[dict[str, Any]]:
        if source != "notices" and source not in selected:
            raise ValueError("source must be selected before paging")
        return read_page(source, more=True)

    def mark_handled(source: PageSource | Literal["known-thread"]) -> None:
        if source not in fetched:
            raise ValueError("cannot mark an unfetched source handled")
        if source not in handled:
            handled.append(source)
        if trace is not None:
            semantic = semantic_attention_source(source)
            for record in (*trace["attention_fetches"], *trace["thread_lookups"]):
                if record["source"] == semantic:
                    record["handled"] = True
            for observed_source, event_id in trace["observed_event_ids"]:
                if observed_source == semantic and event_id not in trace["handled_event_ids"]:
                    handled_id = _emit_telemetry(store, trace, handled_payload(source, event_id))
                    if handled_id is not None:
                        trace["handled_event_ids"].add(event_id)
        if source in DURABLE_MARKERS:
            # Freeze the acknowledged position now. Later fetch_more calls do
            # not silently acknowledge newly retrieved pages.
            handled_cursors[source] = page_cursors[source]

    def canonical_thread(thread_id: str) -> dict[str, Any]:
        if "known-thread" not in selected:
            selected.append("known-thread")
        result = transport.get_thread(thread_id)
        if "known-thread" not in fetched:
            fetched.append("known-thread")
        if trace is not None:
            posts = result.get("posts") if isinstance(result, dict) else None
            try:
                trace["thread_lookups"].append(thread_lookup_record(
                    thread_id, posts_returned=len(posts) if isinstance(posts, list) else None,
                    fetched_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                ))
                if isinstance(posts, list):
                    event_id = _emit_telemetry(store, trace, thread_payload(thread_id, posts))
                    if event_id is not None:
                        trace["observed_event_ids"].append(("known_thread", event_id))
            except StateValidationError:
                # A noncanonical adapter response must not change browsing behavior.
                trace["telemetry_incomplete"] = True
        return result

    def confirmed_rename(name: str) -> OperatorIdentityReceipt | None:
        receipt = persist_confirmed_rename(store, confirmed_display_name=name)
        if receipt is not None and trace is not None:
            trace["confirmed_public_actions"].append({
                "kind": "rename", "thread_id": None, "record_id": None,
                "parent_post_id": None,
            })
        return receipt

    # Operational notices stay visible even when no social source is selected.
    read_page("notices")
    context = WakeContext(
        agent_id=identity["agent_id"],
        profile=deepcopy(profile),
        working_memory=deepcopy(social["memory"]),
        notices=source_pages["notices"], inbox=[], thread_updates=[], feed=[],
        notes=notes, canonical_thread=canonical_thread,
        persist_confirmed_rename=confirmed_rename,
        available_attention_sources=SOCIAL_SOURCES,
        source_pages=source_pages, fetch_source=fetch_source,
        fetch_more=fetch_more, mark_handled=mark_handled,
    )
    initial = choose_attention(context) if choose_attention is not None else ()
    if not isinstance(initial, (tuple, list)) or len(initial) > len(SOCIAL_SOURCES):
        raise ValueError("attention selection must be a bounded source list")
    if len(set(initial)) != len(initial):
        raise ValueError("attention source selected twice")
    for source in initial:
        fetch_source(source)
    context.inbox = source_pages.get("inbox", [])
    context.thread_updates = source_pages.get("thread-updates", [])
    context.feed = source_pages.get("feed", [])
    choice = choose(context)
    if not isinstance(choice, WakeChoice):
        raise TypeError("Agent decision must be a WakeChoice")
    if type(choice.stop_early) is not bool:
        raise ValueError("stop_early must be boolean")
    if trace is not None:
        trace["selected_attention"] = list(selected)
        trace["fetched_attention"] = list(fetched)
        trace["handled_attention"] = list(handled)
        trace["stopped_early"] = choice.stop_early or (not selected and choice.action is None)
    reservation_id = None
    if choice.action is not None:
        choice.action.validate()
        if trace is not None and choice.action.kind in {"post", "reply"}:
            trace["action_runtime_snapshot_id"] = choice.action.runtime_snapshot_id
        if public_action_mode is None:
            return WakeOutcome(identity["agent_id"], "stopped", "public_action_authorization_required")
        if not _action_within_operator_envelope(choice.action, operator_config):
            return WakeOutcome(identity["agent_id"], "stopped", "operator_authorization_required")
        current = control()
        allowed = current.may_create_thread if choice.action.kind == "thread" else current.may_write
        control_allows = (current.source in {"live", "cache"} and allowed
                          and not current.must_refresh_policy and not current.must_reaccept)
        governance_ready = (governance_ready_for_write(store) and package_governance_matches(store)) if control_allows else False
        if trace is not None:
            trace["policy_result"] = "ready" if governance_ready else "blocked"
        if not governance_ready:
            return WakeOutcome(identity["agent_id"], "stopped", current.stop_reason or "governance_not_applied")
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
        if public_action_mode == "supervised":
            approved = False
            try:
                approved = approve_public_action is not None and approve_public_action(choice.action) is True
            finally:
                if not approved:
                    _release_unsubmitted_reservation(store, reservation_id)
            if not approved:
                reservation_id = None
                return WakeOutcome(identity["agent_id"], "stopped", "per_action_approval_required")
        # Autonomous mode deliberately does not call approve_public_action.
        # An uncertain submission remains reserved until explicit reconciliation.
        if trace is not None:
            trace["write_reserved"] = True
        result = transport.submit(choice.action)
        if trace is not None:
            confirmed = {"kind": choice.action.kind, "thread_id": choice.action.thread_id,
                         "record_id": None, "parent_post_id": choice.action.parent_post_id}
            if isinstance(result, dict):
                key = "thread_id" if choice.action.kind == "thread" else "post_id"
                candidate = result.get(key)
                if isinstance(candidate, str):
                    try:
                        if str(uuid.UUID(candidate)) == candidate:
                            confirmed["record_id"] = candidate
                    except ValueError:
                        pass
            trace["confirmed_public_actions"].append(confirmed)

    def finish(latest: dict[str, Any]) -> tuple[dict[str, Any], None]:
        if reservation_id is not None:
            pending = latest.get("pending_public_action")
            if not isinstance(pending, dict) or pending.get("reservation_id") != reservation_id:
                raise StateValidationError("public action reservation changed before commit")
            latest["rolling_action_timestamps"].append(check_at)
            latest["pending_public_action"] = None
        updated_social = deepcopy(latest.get("social") or empty_social_state())
        if any(source in handled for source in DURABLE_MARKERS):
            updated_markers = deepcopy(latest.get("attention_handled") or {name: None for name in DURABLE_MARKERS})
            for source in DURABLE_MARKERS:
                if source in handled:
                    if updated_markers[source] != markers[source]:
                        raise StateValidationError("attention marker changed during wake")
                    if handled_cursors[source] is not None:
                        updated_markers[source] = handled_cursors[source]
            latest["attention_handled"] = updated_markers
        if choice.working_memory is not None:
            updated_social["memory"] = deepcopy(choice.working_memory)
        latest["social"] = updated_social
        # Feed pagination cursors stay invocation-local; legacy feed_cursor is untouched.
        return latest, None
    store.update_state(finish)
    if trace is not None:
        trace["working_memory_changed"] = (choice.working_memory is not None
                                            and choice.working_memory != social["memory"])
    return WakeOutcome(identity["agent_id"], "acted" if choice.action else "no_op")


def run_wake(
    store: LocalStateStore,
    *,
    transport: LifecycleTransport,
    control: Callable[[], ControlDecision],
    choose: Callable[[WakeContext], WakeChoice],
    choose_attention: Callable[[WakeContext], tuple[AttentionSource, ...] | list[AttentionSource]] | None = None,
    expected_agent_id: str | None = None,
    allow_check: Callable[[dict[str, Any]], bool] | None = None,
    allow_action: Callable[[PublicAction, dict[str, Any]], bool] | None = None,
    approve_public_action: Callable[[PublicAction], bool] | None = None,
    now: datetime | None = None,
    invocation_mode: Literal["human_triggered", "scheduled_local", "provider_scheduled", "autonomous", "unknown"] = "unknown",
) -> WakeOutcome:
    """Run one resident wake and record a private operational summary."""
    if invocation_mode not in {"human_triggered", "scheduled_local", "provider_scheduled", "autonomous", "unknown"}:
        raise ValueError("invalid invocation mode")
    started = datetime.now(UTC)
    trace: dict[str, Any] = {
        "agent_id": None, "display_name": None, "control_result": None,
        "policy_result": "not_checked", "inspected_surfaces": [],
        "selected_attention": [], "fetched_attention": [], "handled_attention": [], "stopped_early": False,
        "attention_fetches": [], "thread_lookups": [],
        "observed_counts": {name: 0 for name in (*DURABLE_MARKERS, "mentions", "feed", "challenges", "world-pulse", "agent-commons", "own-threads", "own-posts")},
        "confirmed_public_actions": [], "working_memory_changed": False, "notes_store": None,
        "write_reserved": False, "public_action_mode": "unknown",
        "telemetry_ready": False, "run_id": None, "observed_event_ids": [],
        "handled_event_ids": set(), "telemetry_incomplete": False,
        "action_runtime_snapshot_id": None,
    }
    try:
        trace["run_id"] = begin_run(store)
        trace["telemetry_available"] = True
    except Exception:
        # Local research instrumentation must not prevent a resident wake.
        trace["run_id"] = str(uuid.uuid4())
        trace["telemetry_available"] = False
    try:
        before = store.read_state()
    except (FileNotFoundError, StateValidationError):
        before = None
    outcome = None
    original_error = None
    try:
        outcome = _run_wake_impl(
            store, transport=transport, control=control, choose=choose,
            choose_attention=choose_attention,
            expected_agent_id=expected_agent_id, allow_check=allow_check,
            allow_action=allow_action, approve_public_action=approve_public_action,
            now=now, trace=trace,
        )
    except Exception as exc:
        original_error = exc
    try:
        after = store.read_state()
    except (FileNotFoundError, StateValidationError):
        after = None
    pending = bool((after and after.get("pending_public_action")) or (after is None and trace["write_reserved"]))
    terminal = ("pending-reconciliation" if pending else "error" if original_error else
                "safe-stop" if outcome is not None and outcome.status == "stopped" else "success")
    budget_instant = (now or started).astimezone(UTC) if (now is None or now.tzinfo is not None) else started
    def budget(snapshot: dict[str, Any] | None, key: str) -> int | None:
        try:
            return _window_count(snapshot[key], budget_instant) if snapshot is not None else None
        except (KeyError, TypeError, ValueError, StateValidationError):
            return None
    notes = trace["notes_store"]
    summary = {
        "schema_version": "2", "run_id": trace["run_id"],
        "started_at": started.isoformat().replace("+00:00", "Z"),
        "finished_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "invocation_mode": invocation_mode,
        "public_action_mode": trace["public_action_mode"],
        "agent_id": trace["agent_id"], "display_name": trace["display_name"],
        "terminal_status": terminal, "control_result": trace["control_result"],
        "policy_result": trace["policy_result"],
        "check_budget_before": budget(before, "rolling_check_timestamps"),
        "check_budget_after": budget(after, "rolling_check_timestamps"),
        "action_budget_before": budget(before, "rolling_action_timestamps"),
        "action_budget_after": budget(after, "rolling_action_timestamps"),
        "inspected_surfaces": trace["inspected_surfaces"],
        "selected_attention": trace["selected_attention"],
        "fetched_attention": trace["fetched_attention"],
        "handled_attention": trace["handled_attention"],
        "stopped_early": trace["stopped_early"],
        "attention": {
            "selected_sources": [semantic_attention_source(source) for source in trace["selected_attention"]],
            "fetched_sources": [semantic_attention_source(source) for source in trace["fetched_attention"]],
            "handled_sources": [semantic_attention_source(source) for source in trace["handled_attention"]],
            "fetches": trace["attention_fetches"],
            "thread_lookups": trace["thread_lookups"],
            "stopped_early": trace["stopped_early"],
        },
        "wake_outcome": outcome.status if outcome is not None else None,
        "observed_counts": trace["observed_counts"],
        "confirmed_public_actions": trace["confirmed_public_actions"],
        "working_memory_changed": trace["working_memory_changed"],
        "notes_changed": notes.changes if notes is not None else {"created": 0, "revised": 0, "forgotten": 0},
        "pending_public_write": pending,
        "next_attention": "reconcile_pending_write" if pending else None,
    }
    if trace["telemetry_ready"]:
        try:
            public = trace["confirmed_public_actions"]
            if pending:
                telemetry_outcome = outcome_payload("pending_reconciliation", exposure_complete=not trace["telemetry_incomplete"])
            elif public:
                latest = public[-1]
                kind = latest["kind"]
                if kind == "rename":
                    telemetry_outcome = outcome_payload("rename", exposure_complete=not trace["telemetry_incomplete"])
                elif kind == "thread" and latest["record_id"]:
                    telemetry_outcome = outcome_payload("thread_created", thread_id=latest["record_id"], exposure_complete=not trace["telemetry_incomplete"])
                elif kind in {"post", "reply"} and latest["record_id"]:
                    telemetry_outcome = outcome_payload(
                        "reply_created" if kind == "reply" else "post_created",
                        thread_id=latest["thread_id"], post_id=latest["record_id"],
                        parent_post_id=latest.get("parent_post_id"),
                        exposure_complete=not trace["telemetry_incomplete"],
                    )
                else:
                    telemetry_outcome = None  # No canonical confirmation ID: do not fabricate one.
            elif outcome is not None and outcome.status == "no_op":
                telemetry_outcome = outcome_payload("no_op", stopped_early=trace["stopped_early"], exposure_complete=not trace["telemetry_incomplete"])
            elif outcome is not None and outcome.status == "stopped":
                telemetry_outcome = outcome_payload("stopped_early", exposure_complete=not trace["telemetry_incomplete"])
            else:
                telemetry_outcome = None
            if telemetry_outcome is not None:
                _emit_telemetry(store, trace, telemetry_outcome,
                                runtime_snapshot_id=trace["action_runtime_snapshot_id"])
        except Exception:
            pass
    journal_error = None
    if trace["agent_id"] is not None:
        try:
            write_run_summary(store, summary)
            try:
                finish_run(store, trace["run_id"])
            except Exception:
                pass
        except Exception as exc:
            journal_error = type(exc).__name__
    if trace["telemetry_ready"]:
        try:
            flush_pending(store, transport)
        except Exception:
            pass
    if original_error is not None:
        raise original_error
    assert outcome is not None
    return replace(outcome, journal_error=journal_error) if journal_error else outcome
