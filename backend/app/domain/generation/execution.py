"""Execution modes — capability-first selection policy for the runtime.

The planner requests capabilities; the *execution mode* constrains which tiers of
adapter may satisfy them. Modes are expressed in terms of execution *capability*
(local compute, free remote API, commercial API), never a specific machine or
vendor, so an Intel Mac today, an M1 tomorrow, and an RTX workstation later all
work without any planner change.

``AUTO`` is the cascade the user described: prefer local, else free remote, else
commercial (if allowed), else fail gracefully. ``HYBRID`` allows the same tiers
but does not require stopping at the first available one (useful once a plan
spans multiple capabilities that may each resolve to a different tier).

This module is pure policy: it maps a mode to allowed tiers + a preference order.
The application layer translates that into resolver eligibility flags and adapter
filtering.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ExecutionTier(StrEnum):
    LOCAL = "local"
    FREE_REMOTE = "free_remote"
    COMMERCIAL = "commercial"


class ExecutionMode(StrEnum):
    AUTO = "auto"
    LOCAL_ONLY = "local_only"
    FREE_REMOTE_ONLY = "free_remote_only"
    COMMERCIAL_ONLY = "commercial_only"
    HYBRID = "hybrid"


# Default cascade: cheapest/most-private first.
_CASCADE = (ExecutionTier.LOCAL, ExecutionTier.FREE_REMOTE, ExecutionTier.COMMERCIAL)


@dataclass(frozen=True, slots=True)
class ExecutionConstraints:
    """Which tiers are allowed, in what preference order, and whether to stop early.

    ``stop_at_first_available`` is True for ``AUTO`` (take the first tier that has
    an eligible adapter) and False for ``HYBRID`` (all allowed tiers stay in play).
    ``allows`` is a convenience membership check used by the application layer.
    """

    allowed: tuple[ExecutionTier, ...]
    preference: tuple[ExecutionTier, ...]
    stop_at_first_available: bool

    def allows(self, tier: ExecutionTier) -> bool:
        return tier in self.allowed


def constraints_for(mode: ExecutionMode) -> ExecutionConstraints:
    if mode is ExecutionMode.LOCAL_ONLY:
        tiers = (ExecutionTier.LOCAL,)
        return ExecutionConstraints(tiers, tiers, stop_at_first_available=True)
    if mode is ExecutionMode.FREE_REMOTE_ONLY:
        tiers = (ExecutionTier.FREE_REMOTE,)
        return ExecutionConstraints(tiers, tiers, stop_at_first_available=True)
    if mode is ExecutionMode.COMMERCIAL_ONLY:
        tiers = (ExecutionTier.COMMERCIAL,)
        return ExecutionConstraints(tiers, tiers, stop_at_first_available=True)
    if mode is ExecutionMode.HYBRID:
        return ExecutionConstraints(_CASCADE, _CASCADE, stop_at_first_available=False)
    # AUTO
    return ExecutionConstraints(_CASCADE, _CASCADE, stop_at_first_available=True)
