"""``WorkflowRunStatus`` — the workflow-run lifecycle enum (Slice α7.2).

Framework-free mirror of the physical ``workflow_status`` ENUM (``schema.md`` §16
/ ``enums.py``). Kept as a domain type (not bare string literals) so the use cases
guard transitions in the domain layer. The α7.2 legal transitions are:

    queued ─▶ running ─▶ succeeded
       │        ├─▶ failed
       └────────┴─▶ canceled

``paused`` exists in the physical ENUM but is **not produced** by the α7.2
synchronous runner (pause/resume is deferred to the async driver, α8.x — pre-flight
Q4). It is still modelled here (the enum mirrors the DB) and treated as cancelable
so a pre-existing paused run could be canceled defensively.
"""

from __future__ import annotations

from enum import StrEnum


class WorkflowRunStatus(StrEnum):
    """The six ``workflow_status`` values."""

    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"

    @property
    def is_terminal(self) -> bool:
        """True for states that no longer transition (``succeeded``/``failed``/``canceled``)."""
        return self in _TERMINAL

    @property
    def is_cancelable(self) -> bool:
        """True iff a cancel request may move this state to ``canceled``.

        ``queued`` / ``running`` / ``paused`` are cancelable; ``canceled`` is
        idempotently cancelable (a no-op re-cancel, handled by the use case);
        ``succeeded`` / ``failed`` are **not** (completed work → ``409``, D3.7).
        """
        return self in _CANCELABLE

    @property
    def is_advanceable(self) -> bool:
        """True iff the runner may advance this run (``queued`` start or ``running`` resume)."""
        return self in _ADVANCEABLE


_TERMINAL: frozenset[WorkflowRunStatus] = frozenset(
    {WorkflowRunStatus.SUCCEEDED, WorkflowRunStatus.FAILED, WorkflowRunStatus.CANCELED}
)
_CANCELABLE: frozenset[WorkflowRunStatus] = frozenset(
    {WorkflowRunStatus.QUEUED, WorkflowRunStatus.RUNNING, WorkflowRunStatus.PAUSED}
)
_ADVANCEABLE: frozenset[WorkflowRunStatus] = frozenset(
    {WorkflowRunStatus.QUEUED, WorkflowRunStatus.RUNNING}
)
