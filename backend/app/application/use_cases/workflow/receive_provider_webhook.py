"""``ReceiveProviderWebhook`` — the α8.3b inbound webhook ingress.

The **thin second ingress** ADR-0041 D7 intended. It does exactly three things:

    verify signature  →  find the paused run by provider_job_id  →  CompletionEngine.complete()

and nothing else. Per invariant **W8.3b.1** a webhook handler NEVER directly marks
steps, writes usage, emits workflow events, or resumes runs — all of that stays
inside the already-frozen completion pipeline (``CompletionEngine`` →
``ResumeWorkflowRun`` → ``AdvanceWorkflowRun``). The webhook is a *signal*
("something finished"), not a *result* ("here is the outcome"); ``complete()``
does the authoritative resolve. This use case performs **no writes** — its only
effect is triggering the frozen pipeline.

Exactly-once is owned by ``complete()`` (the ``workflow_run:<id>`` lease + the
``paused → running`` CAS), so duplicate deliveries are inherently safe: a Fal
retry after resume finds no paused run (→ ``noop``); a retry mid-processing hits
the held lease (→ ``locked``). Inbound receipt persistence is intentionally
deferred (see the α8.3b pre-flight Fork D) until a first-class idempotency
subsystem has ≥2 consumers.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

import structlog

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.interfaces.webhook_verifier import IWebhookVerifier
from app.application.use_cases.workflow.completion_engine import CompletionEngine

_LOGGER = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class WebhookIngestResult:
    """The outcome of one inbound webhook.

    ``status`` mirrors the completion outcome where one occurred:

    * ``"resumed"``       — the run resolved terminal and was resumed + driven.
    * ``"in_progress"``   — the provider job is still running; the run stays paused.
    * ``"noop"``          — the run was not paused (already handled) — safe duplicate.
    * ``"locked"``        — another ingress holds the lease; this delivery is a no-op.
    * ``"unknown_job"``   — no paused run matches this provider job id (ack, stop retries).
    * ``"unsupported"``   — no verifier registered for the path ``{provider}`` (→ 404).
    """

    status: str
    workflow_run_id: UUID | None = None
    run_status: str | None = None


class ReceiveProviderWebhook:
    """Verify a provider webhook and trigger the frozen completion pipeline."""

    def __init__(
        self,
        uow: IUnitOfWork,
        completion_engine: CompletionEngine,
        verifiers: Mapping[str, IWebhookVerifier],
    ) -> None:
        # ``uow`` is used ONLY to read (find the paused run). ``verifiers`` is a
        # per-provider registry (α8.3b ships ``{"fal": FalWebhookVerifier}``). The
        # completion engine owns its own transactions + lease.
        self._uow = uow
        self._completion_engine = completion_engine
        self._verifiers = verifiers

    async def execute(
        self, *, provider: str, body: bytes, headers: Mapping[str, str]
    ) -> WebhookIngestResult:
        """Verify → locate paused run → ``complete()``.

        Raises ``WebhookVerificationError`` (→ 401) / ``WebhookMalformedError`` (→ 400)
        from the verifier; those are intentionally *not* caught here so the router
        maps them to status codes. Returns a :class:`WebhookIngestResult` otherwise.
        """
        verifier = self._verifiers.get(provider)
        if verifier is None:
            _LOGGER.info("webhook.unsupported_provider", provider=provider)
            return WebhookIngestResult(status="unsupported")

        # Authenticate + extract the ONLY trusted datum: the provider job id.
        verified = await verifier.verify(body=body, headers=headers)
        provider_job_id = verified.provider_job_id

        async with self._uow:
            run = await self._uow.workflow_runs.find_paused_by_provider_job_id(provider_job_id)

        if run is None:
            # Unknown / already-resolved job — ack so the provider stops retrying.
            _LOGGER.info(
                "webhook.no_paused_run", provider=provider, provider_job_id=provider_job_id
            )
            return WebhookIngestResult(status="unknown_job")

        # Trigger the frozen, idempotent completion pipeline. No state is written here.
        outcome = await self._completion_engine.complete(
            project_id=run.project_id, workflow_run_id=run.id
        )
        _LOGGER.info(
            "webhook.completion_triggered",
            provider=provider,
            provider_job_id=provider_job_id,
            workflow_run_id=str(run.id),
            outcome=outcome.status,
        )
        return WebhookIngestResult(
            status=outcome.status,
            workflow_run_id=outcome.workflow_run_id,
            run_status=outcome.run_status,
        )
