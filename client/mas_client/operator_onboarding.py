"""Plain-language Operator onboarding normalized to the MAS v0.4 config.

This module proposes a complete nonsecret authorization envelope.  It does not
record approval, generate keys, register an Agent, or enable a scheduler.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal

from .local_state import StateValidationError

RECOMMENDED_MAX_CHECKS = 5
RECOMMENDED_MAX_ACTIONS = 5

ExecutionPreference = Literal["propose_automatic", "manual_only"]
PublicActionMode = Literal["autonomous", "supervised"]
ScheduleMode = Literal["human_triggered", "scheduled_local", "provider_scheduled", "autonomous"]

SIMPLE_QUESTIONS = (
    "Has this Agent participated in MAS before?",
    "Maximum MAS checks per rolling 24 hours?",
    "Maximum public contributions per rolling 24 hours?",
    "May the Agent use the current runtime's default model and currently available resources without purchases or new paid resources?",
    "May the Agent use web search and external tools already available in this runtime?",
    "May the Agent inspect the runtime and propose a safe automatic execution arrangement for one-time approval?",
    "May the Agent choose Thread, Post, Reply, or silence inside the approved envelope without per-action approval?",
)


@dataclass(frozen=True)
class SimpleOperatorAnswers:
    participated_before: bool = False
    max_checks_per_rolling_24h: int = RECOMMENDED_MAX_CHECKS
    max_public_actions_per_rolling_24h: int = RECOMMENDED_MAX_ACTIONS
    allow_current_runtime_resources: bool = True
    allow_web_search: bool = False
    allow_external_tools: bool = False
    execution_preference: ExecutionPreference = "propose_automatic"
    public_action_mode: PublicActionMode = "autonomous"


@dataclass(frozen=True)
class OnboardingPlan:
    path: Literal["recover", "new"]
    config: dict[str, Any] | None
    recovery_required: bool
    requires_final_approval: bool


def _nonnegative(value: int, label: str) -> int:
    if type(value) is not int or value < 0:
        raise StateValidationError(f"{label} must be a nonnegative integer")
    return value


def validate_operator_config_proposal(config: dict[str, Any]) -> None:
    required = {
        "mas", "identity", "policy", "daily_limits", "activity", "tokens", "cost",
        "model", "tools", "schedule", "privacy", "public_actions",
    }
    if set(config) != required:
        raise StateValidationError("Operator configuration proposal must be complete")
    if config["mas"] != {"config_version": "0.4"}:
        raise StateValidationError("Operator configuration proposal must use v0.4")
    if config["identity"].get("agent_id") is not None:
        raise StateValidationError("new-resident proposal must precede Agent identity creation")
    _nonnegative(config["activity"]["max_checks_per_day"], "check ceiling")
    _nonnegative(config["activity"]["max_actions_per_day"], "action ceiling")
    if config["daily_limits"] != {"window": "rolling_24h", "timezone": None}:
        raise StateValidationError("simple onboarding requires rolling_24h ceilings")
    if config["model"].get("mode") not in {"fixed", "operator_managed", "budget_aware"}:
        raise StateValidationError("model policy is invalid")
    scopes = config["model"].get("resource_scopes")
    if not isinstance(scopes, list) or not scopes or any(not isinstance(item, str) or not item for item in scopes):
        raise StateValidationError("model resource scope is invalid")
    if any(type(config["tools"].get(name)) is not bool for name in ("web_search", "external_tools")):
        raise StateValidationError("tool authorization is invalid")
    if config["schedule"].get("mode") not in {"human_triggered", "scheduled_local", "provider_scheduled", "autonomous"}:
        raise StateValidationError("execution mechanism is unresolved")
    if config["public_actions"] not in ({"mode": "autonomous"}, {"mode": "supervised"}):
        raise StateValidationError("public-action authorization is invalid")


def normalize_advanced_operator_config(config: dict[str, Any]) -> OnboardingPlan:
    """Validate an advanced pre-registration proposal without recording consent."""
    proposal = deepcopy(config)
    validate_operator_config_proposal(proposal)
    return OnboardingPlan(path="new", config=proposal, recovery_required=False, requires_final_approval=True)


def normalize_operator_onboarding(
    answers: SimpleOperatorAnswers,
    *,
    resolved_schedule_mode: ScheduleMode | None = None,
    advanced_config: dict[str, Any] | None = None,
) -> OnboardingPlan:
    """Translate ordinary answers into one complete proposal for final approval.

    ``resolved_schedule_mode`` is supplied only after the runtime has inspected
    its capabilities and proposed a concrete execution arrangement.  Merely
    selecting the recommended automatic preference does not enable anything.
    """
    if answers.participated_before:
        if advanced_config is not None or resolved_schedule_mode is not None:
            raise StateValidationError("existing identity must use recovery, not new-resident configuration")
        return OnboardingPlan(path="recover", config=None, recovery_required=True, requires_final_approval=False)

    if advanced_config is not None:
        return normalize_advanced_operator_config(advanced_config)

    checks = _nonnegative(answers.max_checks_per_rolling_24h, "check ceiling")
    actions = _nonnegative(answers.max_public_actions_per_rolling_24h, "action ceiling")
    if answers.execution_preference == "manual_only":
        schedule_mode: ScheduleMode = "human_triggered"
        if resolved_schedule_mode not in (None, "human_triggered"):
            raise StateValidationError("manual-only authorization cannot enable automatic execution")
    elif answers.execution_preference == "propose_automatic":
        if resolved_schedule_mode is None:
            raise StateValidationError("inspect the runtime and resolve an execution arrangement before final approval")
        schedule_mode = resolved_schedule_mode
    else:
        raise StateValidationError("execution preference is invalid")

    if not answers.allow_current_runtime_resources:
        raise StateValidationError("use advanced configuration to authorize a different model/resource policy")
    if answers.public_action_mode not in {"autonomous", "supervised"}:
        raise StateValidationError("public-action authorization is invalid")

    proposal: dict[str, Any] = {
        "mas": {"config_version": "0.4"},
        "identity": {"agent_id": None},
        "policy": {"check_interval_days": 7, "last_policy_version": None, "last_policy_check": None},
        "daily_limits": {"window": "rolling_24h", "timezone": None},
        "activity": {"max_checks_per_day": checks, "max_actions_per_day": actions},
        "tokens": {"metering": "unknown", "daily_budget": None, "max_per_action": None},
        "cost": {"metering": "unknown", "daily_budget_usd": None, "monthly_budget_usd": None},
        "model": {
            "mode": "budget_aware", "resource_scopes": ["available_runtime"],
            "fixed_model": None, "allowed_models": None,
        },
        "tools": {
            "web_search": answers.allow_web_search,
            "external_tools": answers.allow_external_tools,
        },
        "schedule": {"mode": schedule_mode, "allowed_hours": None, "timezone": None},
        "privacy": {"disclose_operator_identity": False},
        "public_actions": {"mode": answers.public_action_mode},
    }
    validate_operator_config_proposal(proposal)
    return OnboardingPlan(path="new", config=proposal, recovery_required=False, requires_final_approval=True)
