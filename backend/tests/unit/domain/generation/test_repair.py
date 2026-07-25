"""Unit tests for the pure repair policy."""

from __future__ import annotations

import pytest

from app.domain.generation.repair import RepairAction, decide_repair
from app.domain.generation.verification import CheckResult, CheckStatus, VerificationReport

pytestmark = pytest.mark.unit


def _passing() -> VerificationReport:
    return VerificationReport((CheckResult("produced", CheckStatus.PASS),))


def _failing() -> VerificationReport:
    return VerificationReport(
        (
            CheckResult("produced", CheckStatus.PASS),
            CheckResult("not_blank", CheckStatus.FAIL, "image is blank"),
        )
    )


def test_passing_report_is_accepted() -> None:
    decision = decide_repair(_passing(), attempt=1, current_seed=10)
    assert decision.action is RepairAction.ACCEPT


def test_failure_with_attempts_left_retries_with_new_seed() -> None:
    decision = decide_repair(_failing(), attempt=1, current_seed=10, max_attempts=3)
    assert decision.action is RepairAction.RETRY
    assert decision.next_seed == 11  # current_seed + attempt
    assert "blank" in decision.reason


def test_second_retry_derives_distinct_seed() -> None:
    decision = decide_repair(_failing(), attempt=2, current_seed=10, max_attempts=3)
    assert decision.action is RepairAction.RETRY
    assert decision.next_seed == 12


def test_failure_at_max_attempts_gives_up() -> None:
    decision = decide_repair(_failing(), attempt=3, current_seed=10, max_attempts=3)
    assert decision.action is RepairAction.GIVE_UP
    assert decision.next_seed is None


def test_max_two_retries_default() -> None:
    # Default DEFAULT_MAX_ATTEMPTS == 3 => attempts 1,2 retry; attempt 3 gives up.
    assert decide_repair(_failing(), attempt=1, current_seed=1).action is RepairAction.RETRY
    assert decide_repair(_failing(), attempt=2, current_seed=1).action is RepairAction.RETRY
    assert decide_repair(_failing(), attempt=3, current_seed=1).action is RepairAction.GIVE_UP
