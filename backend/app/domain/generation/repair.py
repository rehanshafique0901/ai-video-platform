"""Repair policy — decide what to do after a shot is verified.

Pure and deterministic: given a verification report and how many attempts have
been made, decide whether to accept the shot, retry it (with a fresh seed to
escape a bad generation), or give up. Repair regenerates only the failed shot,
never the whole project.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.domain.generation.verification import VerificationReport

# One initial attempt + this many retries (the user's "max 2 retries").
DEFAULT_MAX_ATTEMPTS = 3


class RepairAction(StrEnum):
    ACCEPT = "accept"
    RETRY = "retry"
    GIVE_UP = "give_up"


@dataclass(frozen=True, slots=True)
class RepairDecision:
    action: RepairAction
    next_seed: int | None = None
    reason: str = ""


def decide_repair(
    report: VerificationReport,
    *,
    attempt: int,
    current_seed: int,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> RepairDecision:
    """Decide the next step for a shot.

    ``attempt`` is 1-based (the just-completed attempt number). A passing report
    is always accepted; otherwise we retry while attempts remain, deriving a new
    seed deterministically so the retry differs from the failed generation.
    """
    if report.passed:
        return RepairDecision(RepairAction.ACCEPT, reason="all checks passed")

    reasons = "; ".join(f"{c.name}: {c.detail}" for c in report.failures) or "verification failed"
    if attempt >= max_attempts:
        return RepairDecision(
            RepairAction.GIVE_UP, reason=f"exhausted {max_attempts} attempts ({reasons})"
        )
    return RepairDecision(
        RepairAction.RETRY,
        next_seed=current_seed + attempt,
        reason=f"retry after failure ({reasons})",
    )
