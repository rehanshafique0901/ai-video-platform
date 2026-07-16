"""Workflow domain entities — the Workflow-orchestration aggregate (α7.2).

**Slim projections** of the ``workflow_runs`` / ``workflow_steps`` /
``workflow_checkpoints`` tables (``schema.md`` §16 / ``models/workflows.py``).
Frozen for value-semantics (mutations return new instances at the repository/use-
case layer) — the same discipline as :class:`app.domain.render.render_job.RenderJob`.

Key modelling decisions (WORKFLOW_RUN_AGGREGATE.md / ADR-0040 / α7.2 pre-flight):

* **No ``version`` column (D3.2).** Neither ``workflow_runs`` nor ``workflow_steps``
  carries a ``version`` (they are not in ``_VERSION_BUMP_TABLES``); concurrency is
  status-guarded CAS, not numeric OCC. There is therefore **no ``version`` field**
  on :class:`WorkflowRun` / :class:`WorkflowStep` (unlike ``RenderJob``).
* **Ownership is derived through the project** (``project_id → projects.owner_user_id``);
  the run carries no ``owner_user_id`` / ``tenant_id``.
* **No soft-delete** — a run is an operationally terminal audit record. "Removal"
  is the ``canceled`` status, not a delete.
* **Checkpoints are append-only (ADR-0014)** — :class:`WorkflowCheckpoint` has only
  ``created_at`` (``CreatedAtOnlyMixin``); the DB ``reject_mutation`` trigger blocks
  UPDATE/DELETE. Written once, never mutated.
* **Owns only orchestration/graph state (D3.10)** — run status, the ordered step
  sequence + status, and checkpoints. Rendered files, render lifecycle, and
  timeline edits live on their own aggregates.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(frozen=True, slots=True)
class WorkflowRun:
    """Workflow aggregate root — one row of the ``workflow_runs`` table (slim view)."""

    id: UUID
    project_id: UUID
    workflow_key: str
    workflow_version: str
    status: str
    started_at: datetime | None
    finished_at: datetime | None
    triggered_by_user_id: UUID | None
    idempotency_key: str | None
    input_snapshot: dict[str, Any]
    output_summary: dict[str, Any] | None
    error: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class WorkflowStep:
    """One ordered child of a run — a row of the ``workflow_steps`` table (slim view)."""

    id: UUID
    workflow_run_id: UUID
    step_index: int
    step_name: str
    status: str
    started_at: datetime | None
    finished_at: datetime | None
    retries: int
    input: dict[str, Any] | None
    output: dict[str, Any] | None
    error: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class WorkflowCheckpoint:
    """An append-only resume point — a row of the ``workflow_checkpoints`` table."""

    id: UUID
    workflow_run_id: UUID
    step_index: int
    state: dict[str, Any]
    created_at: datetime
