"""``WorkflowStepStatus`` — the workflow-step lifecycle enum (Slice α7.2).

Framework-free mirror of the physical ``step_status`` ENUM (``schema.md`` §16 /
``enums.py``). The α7.2 legal transitions are:

    pending ─▶ running ─▶ succeeded
                 │  ▲──── retrying   (transient failure, up to the definition bound)
                 └─▶ failed

``skipped`` exists in the physical ENUM for definition-level conditional skips; the
α7.2 deterministic test workflows do not emit it, but a resumed runner treats a
``skipped`` step like a ``succeeded`` one (no more work to do — :prop:`is_done`).
"""

from __future__ import annotations

from enum import StrEnum


class WorkflowStepStatus(StrEnum):
    """The six ``step_status`` values."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    RETRYING = "retrying"

    @property
    def is_terminal(self) -> bool:
        """True for states the runner no longer acts on (``succeeded``/``failed``/``skipped``)."""
        return self in _TERMINAL

    @property
    def is_done(self) -> bool:
        """True iff the step needs no further work on a resume (``succeeded``/``skipped``)."""
        return self in _DONE

    @property
    def is_runnable(self) -> bool:
        """True iff the runner may (re)start this step (``pending``/``retrying``)."""
        return self in _RUNNABLE


_TERMINAL: frozenset[WorkflowStepStatus] = frozenset(
    {WorkflowStepStatus.SUCCEEDED, WorkflowStepStatus.FAILED, WorkflowStepStatus.SKIPPED}
)
_DONE: frozenset[WorkflowStepStatus] = frozenset(
    {WorkflowStepStatus.SUCCEEDED, WorkflowStepStatus.SKIPPED}
)
_RUNNABLE: frozenset[WorkflowStepStatus] = frozenset(
    {WorkflowStepStatus.PENDING, WorkflowStepStatus.RETRYING}
)
