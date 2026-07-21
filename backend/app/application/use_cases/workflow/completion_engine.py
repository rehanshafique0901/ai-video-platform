"""``CompletionEngine`` — async job completion for paused runs (Slice α8.3).

The α7.6 pause seam leaves a run ``paused`` with an in-flight provider job; this
engine closes the loop. It is the completion **engine** the roadmap called for: one
public method (:meth:`complete`) that every ingress converges on. α8.3 ships the
**polling** ingress (:meth:`poll_once`); the α8.3b webhook receiver will be a thin
second ingress calling the same :meth:`complete`.

    poll_once ─┐
               ├─► complete() ─► ResumeWorkflowRun ─► AdvanceWorkflowRun
    (webhook) ─┘

:meth:`complete` runs under a per-run distributed **lease**
(``workflow_run:<id>``) so two ingresses (poll racing a future webhook) never
resolve the same job concurrently — and even if a lease is stolen after expiry,
the resume's CAS (``paused → running``) is the exactly-once backstop. Under the
lease it: reads the ``_paused`` handoff, hands the opaque envelope to the provider's
``resolve`` (via :meth:`ProviderDispatcherPort.resolve_job`), leaves the run paused
if still ``IN_PROGRESS``, or delegates a terminal result to :class:`ResumeWorkflowRun`.

Library-only (no Celery / Redis / daemon): a test loop or trigger drives
:meth:`poll_once` / :meth:`complete`. Provider I/O happens **outside** any DB
transaction (the lease + CAS, not a long-held row lock, provide isolation).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

import structlog

from app.application.interfaces.provider_dispatcher import ProviderDispatcherPort
from app.application.interfaces.providers import Capability, ProviderStatus
from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.workflow.resume_workflow_run import ResumeWorkflowRun
from app.domain.workflow.workflow_run_status import WorkflowRunStatus

_LOGGER = structlog.get_logger(__name__)

# Default per-run lease — comfortably longer than a resolve round-trip, short enough
# that a crashed holder's lock is reclaimable on the next tick. Config-overridable.
_DEFAULT_LEASE = timedelta(seconds=60)


@dataclass(frozen=True, slots=True)
class CompletionOutcome:
    """The result of one :meth:`CompletionEngine.complete` attempt.

    ``status`` is one of:

    * ``"resumed"``      — the job resolved terminal and the run was resumed + driven.
    * ``"in_progress"``  — the provider job is still running; the run stays paused.
    * ``"noop"``         — the run was not paused (already handled) — idempotent replay.
    * ``"locked"``       — another ingress holds the lease; skipped this tick.
    """

    workflow_run_id: UUID
    status: str
    run_status: str | None = None


@dataclass(frozen=True, slots=True)
class CompletionPollResult:
    """Aggregate of one :meth:`CompletionEngine.poll_once` sweep over all paused runs."""

    scanned: int
    outcomes: list[CompletionOutcome]


class CompletionEngine:
    """Resolve in-flight provider jobs for paused runs and resume the terminal ones."""

    def __init__(
        self,
        uow: IUnitOfWork,
        resume: ResumeWorkflowRun,
        dispatcher: ProviderDispatcherPort,
        *,
        owner: str,
        lease: timedelta = _DEFAULT_LEASE,
    ) -> None:
        # ``uow`` reads paused runs + checkpoints and drives the lease (uow.locks); it
        # is re-entered per scope. ``resume`` owns its OWN uow (the atomic resume txn),
        # so completion (lease + read) and resume are independent transactions.
        self._uow = uow
        self._resume = resume
        self._dispatcher = dispatcher
        self._owner = owner
        self._lease = lease

    async def poll_once(self) -> CompletionPollResult:
        """Poll every ``paused`` run once, resolving + resuming the terminal ones.

        The α8.3 polling ingress. Oldest pause first (repository order). Each run is
        completed independently under its own lease, so one stuck/slow provider does
        not block the others.
        """
        async with self._uow:
            paused = await self._uow.workflow_runs.list_paused()

        outcomes: list[CompletionOutcome] = []
        for run in paused:
            outcomes.append(await self.complete(project_id=run.project_id, workflow_run_id=run.id))
        return CompletionPollResult(scanned=len(paused), outcomes=outcomes)

    async def complete(self, *, project_id: UUID, workflow_run_id: UUID) -> CompletionOutcome:
        """Resolve one paused run's provider job under its lease; resume if terminal.

        The single public completion entrypoint (poll now, webhook in α8.3b). Idempotent
        and exactly-once: the lease serialises ingresses and the resume's CAS is the
        backstop, so a duplicated call is at worst a wasted resolve, never a double resume.
        """
        lock_key = f"workflow_run:{workflow_run_id}"
        async with self._uow:
            lease = await self._uow.locks.acquire(
                key=lock_key, owner=self._owner, lease=self._lease
            )
            await self._uow.commit()
        if lease is None:
            _LOGGER.info("completion.locked", workflow_run_id=str(workflow_run_id))
            return CompletionOutcome(workflow_run_id=workflow_run_id, status="locked")

        try:
            resolve_input = await self._read_resolve_input(project_id, workflow_run_id)
            if resolve_input is None:
                return CompletionOutcome(workflow_run_id=workflow_run_id, status="noop")

            capability, provider_job_id, envelope = resolve_input
            resolved = await self._dispatcher.resolve_job(
                capability, provider_job_id=provider_job_id, envelope=envelope
            )

            if resolved.status is ProviderStatus.IN_PROGRESS:
                _LOGGER.info(
                    "completion.in_progress",
                    workflow_run_id=str(workflow_run_id),
                    provider_job_id=provider_job_id,
                )
                return CompletionOutcome(
                    workflow_run_id=workflow_run_id, status="in_progress", run_status="paused"
                )

            result = await self._resume.execute(
                project_id=project_id, workflow_run_id=workflow_run_id, resolved=resolved
            )
            return CompletionOutcome(
                workflow_run_id=workflow_run_id,
                status="resumed" if result.resumed else "noop",
                run_status=result.view.run.status,
            )
        finally:
            async with self._uow:
                await self._uow.locks.release(lease)
                await self._uow.commit()

    async def _read_resolve_input(
        self, project_id: UUID, workflow_run_id: UUID
    ) -> tuple[Capability, str, dict[str, Any]] | None:
        """Read the ``_paused`` handoff for the resolve call, or ``None`` to skip.

        Returns ``(capability, provider_job_id, envelope)``. ``None`` when the run is
        not ``paused`` (already handled), has no provider job, or lacks the handoff —
        all no-ops for the poller.
        """
        async with self._uow:
            run = await self._uow.workflow_runs.get_owned(project_id, workflow_run_id)
            if run is None or WorkflowRunStatus(run.status) is not WorkflowRunStatus.PAUSED:
                return None
            checkpoint = await self._uow.workflow_runs.latest_checkpoint(run.id)
            state = checkpoint.state if checkpoint is not None else None
            paused = state.get("_paused") if isinstance(state, dict) else None
            if not isinstance(paused, dict):
                _LOGGER.warning("completion.no_handoff", workflow_run_id=str(workflow_run_id))
                return None
            provider_job_id = paused.get("provider_job_id")
            if not isinstance(provider_job_id, str) or not provider_job_id:
                _LOGGER.warning("completion.no_provider_job", workflow_run_id=str(workflow_run_id))
                return None
            try:
                capability = Capability(str(paused["capability"]))
            except (KeyError, ValueError):
                _LOGGER.warning("completion.bad_capability", workflow_run_id=str(workflow_run_id))
                return None
            envelope = paused.get("envelope")
            envelope = dict(envelope) if isinstance(envelope, dict) else {}
            return capability, provider_job_id, envelope
