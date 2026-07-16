"""Shared read-model for the detail-returning workflow use cases (α7.2).

``create`` / ``get`` / ``advance`` / ``cancel`` all return the run **plus** its
ordered steps and the latest checkpoint, so the router can project one consistent
detail DTO. ``list`` returns bare runs (a lighter summary projection).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.workflow.workflow_run import WorkflowCheckpoint, WorkflowRun, WorkflowStep


@dataclass(frozen=True, slots=True)
class WorkflowRunView:
    """A workflow run with its ordered steps and latest checkpoint (the detail read-model)."""

    run: WorkflowRun
    steps: list[WorkflowStep]
    latest_checkpoint: WorkflowCheckpoint | None
