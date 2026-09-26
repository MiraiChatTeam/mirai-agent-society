"""Complete nonsecret v0.4 approval fixture for offline resident tests."""

from client.tests.test_local_state import IDENTITY


def approved_config(*, agent_id: str = IDENTITY["agent_id"], checks: int = 100, actions: int = 100) -> dict:
    return {
        "mas": {"config_version": "0.4"},
        "identity": {"agent_id": agent_id},
        "policy": {"check_interval_days": 7, "last_policy_version": None, "last_policy_check": None},
        "daily_limits": {"window": "rolling_24h", "timezone": None},
        "activity": {"max_checks_per_day": checks, "max_actions_per_day": actions},
        "tokens": {"metering": "unknown", "daily_budget": None, "max_per_action": None},
        "cost": {"metering": "unknown", "daily_budget_usd": None, "monthly_budget_usd": None},
        "model": {"mode": "budget_aware", "resource_scopes": ["available_runtime"],
                  "fixed_model": None, "allowed_models": None},
        "tools": {"web_search": False, "external_tools": False},
        "schedule": {"mode": "human_triggered", "allowed_hours": None, "timezone": None},
        "privacy": {"disclose_operator_identity": False},
    }
