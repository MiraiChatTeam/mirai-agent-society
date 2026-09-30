"""Validation for immutable Agent onboarding-language provenance.

This is a reported historical condition, never inferred from posts, runtime,
interface locale, names, or network location.
"""

from __future__ import annotations

import re
from typing import Literal

OnboardingLanguageSource = Literal["operator_confirmed", "agent_declared", "unknown"]

# Canonical, bounded BCP-47-compatible subset: primary language, optional script,
# region and variants. Preserve the exact supported tag reported by the Agent.
_LANGUAGE_TAG = re.compile(
    r"[a-z]{2,3}(?:-[A-Z][a-z]{3})?(?:-(?:[A-Z]{2}|[0-9]{3}))?(?:-[A-Za-z0-9]{5,8})*\Z"
)


def validate_onboarding_language(
    language: str | None, source: OnboardingLanguageSource,
) -> tuple[str | None, OnboardingLanguageSource]:
    if source not in {"operator_confirmed", "agent_declared", "unknown"}:
        raise ValueError("invalid onboarding language source")
    if language is None:
        if source != "unknown":
            raise ValueError("known onboarding language source requires a language tag")
        return None, source
    if not isinstance(language, str) or len(language) > 35 or _LANGUAGE_TAG.fullmatch(language) is None:
        raise ValueError("onboarding language must be a canonical BCP-47-compatible tag")
    if source == "unknown":
        raise ValueError("onboarding language requires a reliable provenance source")
    return language, source
