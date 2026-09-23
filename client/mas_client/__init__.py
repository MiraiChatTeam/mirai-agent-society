"""Local, vendor-neutral MAS client foundations (not an Agent Skill)."""

from .capability import CapabilityEvidence, capability_level
from .local_state import (
    IdentityConflictError,
    IdentityMissingError,
    LocalStateStore,
    StateValidationError,
)

__all__ = [
    "CapabilityEvidence",
    "IdentityConflictError",
    "IdentityMissingError",
    "LocalStateStore",
    "StateValidationError",
    "capability_level",
]
