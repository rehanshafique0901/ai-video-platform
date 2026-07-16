"""DTOs for ``/api/v1/projects/{project_id}/workflow-runs/*`` endpoints (α7.2).

A **workflow run** is the record of one workflow execution and the orchestration
graph beneath it (ADR-0040):

* :class:`WorkflowRunCreateRequest` — ``POST`` body. ``workflow_key`` /
  ``workflow_version`` name an in-code workflow definition (unknown → ``422``);
  ``input_snapshot`` is the frozen request inputs (defaults to ``{}``);
  ``idempotency_key`` enables idempotent replay (Q7). ``extra="forbid"`` turns any
  non-declared key into a ``422`` — status/lifecycle fields are server-owned.
* :class:`WorkflowStepPublic` / :class:`WorkflowCheckpointPublic` — child
  projections included in the run detail.
* :class:`WorkflowRunSummary` — the list-item projection (run scalars only).
* :class:`WorkflowRunPublic` — the detail projection (run + ordered ``steps`` +
  ``latest_checkpoint``). There is **no ``version`` field** — ``workflow_runs`` has
  no OCC token (D3.2); cancel/advance are status-guarded, not version-fenced.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# Physical ``workflow_status`` ENUM (``enums.py`` / baseline 0001) — validates the
# ``?status=`` list filter.
WorkflowStatusLiteral = Literal["queued", "running", "paused", "succeeded", "failed", "canceled"]

_MAX_TEXT = 2_048


class WorkflowRunCreateRequest(BaseModel):
    """POST /api/v1/projects/{project_id}/workflow-runs body.

    ``workflow_key`` + ``workflow_version`` must resolve to a registered in-code
    workflow definition (else ``422``). ``input_snapshot`` is the frozen inputs the
    run executes against (defaults to ``{}``). ``idempotency_key`` (when supplied)
    makes the create idempotent for this project (Q7).
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    workflow_key: str = Field(min_length=1, max_length=_MAX_TEXT)
    workflow_version: str = Field(min_length=1, max_length=_MAX_TEXT)
    input_snapshot: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=255)


class WorkflowStepPublic(BaseModel):
    """Public projection of :class:`app.domain.workflow.workflow_run.WorkflowStep`."""

    id: UUID
    step_index: int
    step_name: str
    status: str
    started_at: datetime | None
    finished_at: datetime | None
    retries: int
    output: dict[str, Any] | None
    error: dict[str, Any] | None


class WorkflowCheckpointPublic(BaseModel):
    """Public projection of :class:`app.domain.workflow.workflow_run.WorkflowCheckpoint`."""

    id: UUID
    step_index: int
    state: dict[str, Any]
    created_at: datetime


class WorkflowRunSummary(BaseModel):
    """List-item projection — run scalars only (no steps)."""

    id: UUID
    project_id: UUID
    workflow_key: str
    workflow_version: str
    status: str
    started_at: datetime | None
    finished_at: datetime | None
    triggered_by_user_id: UUID | None
    idempotency_key: str | None
    output_summary: dict[str, Any] | None
    error: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


class WorkflowRunPublic(WorkflowRunSummary):
    """Detail projection — the run plus its ordered steps, inputs, and latest checkpoint."""

    input_snapshot: dict[str, Any]
    steps: list[WorkflowStepPublic]
    latest_checkpoint: WorkflowCheckpointPublic | None
