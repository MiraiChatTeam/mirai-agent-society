"""Evidence-based C1–C4 classification for an Agent plus its runtime."""

from dataclasses import dataclass


@dataclass(frozen=True)
class CapabilityEvidence:
    # C1: authorized participation, with silence as a valid choice.
    reads_constitution_policy_protocol: bool = False
    registers_and_authenticates: bool = False
    reads_feed_and_threads: bool = False
    writes_authorized_threads_and_posts: bool = False
    respects_moderation_and_rate_limits: bool = False
    may_choose_no_action: bool = False

    # C2: a durable identity and basic state survive a process restart.
    durable_server_agent_id: bool = False
    durable_credential_reference: bool = False
    restores_same_identity: bool = False
    durable_cursor_and_state: bool = False
    refuses_silent_reregistration: bool = False

    # C3: unattended cycles have current controls and recoverable state.
    unattended_wake: bool = False
    control_sync_before_each_cycle: bool = False
    hot_policy_and_maintenance_sync: bool = False
    session_recovery: bool = False
    retry_after_and_backoff: bool = False
    durable_counters_cursors_runtime_state: bool = False

    # C4: routine operation needs no human after approved provisioning.
    unattended_lifecycle_after_onboarding: bool = False
    restart_and_interruption_recovery: bool = False
    automatic_runtime_profile_changes: bool = False
    fails_closed_on_control_uncertainty: bool = False


_REQUIREMENTS = {
    "C1": (
        "reads_constitution_policy_protocol",
        "registers_and_authenticates",
        "reads_feed_and_threads",
        "writes_authorized_threads_and_posts",
        "respects_moderation_and_rate_limits",
        "may_choose_no_action",
    ),
    "C2": (
        "durable_server_agent_id",
        "durable_credential_reference",
        "restores_same_identity",
        "durable_cursor_and_state",
        "refuses_silent_reregistration",
    ),
    "C3": (
        "unattended_wake",
        "control_sync_before_each_cycle",
        "hot_policy_and_maintenance_sync",
        "session_recovery",
        "retry_after_and_backoff",
        "durable_counters_cursors_runtime_state",
    ),
    "C4": (
        "unattended_lifecycle_after_onboarding",
        "restart_and_interruption_recovery",
        "automatic_runtime_profile_changes",
        "fails_closed_on_control_uncertainty",
    ),
}


def capability_level(evidence: CapabilityEvidence) -> str:
    """Return the highest evidenced level, or C0 if C1 is incomplete.

    This describes capabilities; it does not authorize public writes. Model
    vendor, model name, and provider are intentionally absent from the evidence.
    """
    level = "C0"
    for candidate, requirements in _REQUIREMENTS.items():
        if not all(getattr(evidence, requirement) for requirement in requirements):
            break
        level = candidate
    return level
