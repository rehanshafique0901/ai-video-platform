"""``UsageRecorderService`` — the Usage Recorder (Slice α7.5).

Turns one **terminal** provider call into exactly one immutable, priced
``usage_records`` row (ADR-0019 / ADR-0041 D13), idempotent on ``request_id``
(ADR-0033), priced from ``ai_model_pricing`` (CR-11). One :meth:`record` call is
**one transaction** over one row: account → price → insert → commit.

Sign-off invariants enforced here:

* **Q6 (terminal only):** an ``IN_PROGRESS`` command is rejected — the α8.3
  completion service records the terminal outcome later under the same
  ``request_id``.
* **Q5 (never block):** missing pricing prices the affected units at 0, leaves
  ``pricing_id`` NULL, and emits a ``WARN`` — recording never fails the workflow.
* **Q7 (idempotent):** a colliding ``request_id`` (ADR-0033 unique) is recovered by
  returning the pre-existing row (``idempotent_replay=True``).
* **Q1 (no ledger):** ``credits_consumed`` is left at 0 — the ``credit_ledger``
  debit is a later slice.
* **W7.5.1 (observational):** the only write is ``usage_records`` — the service
  touches no aggregate repository (``uow.usage`` + ``uow.model_pricing`` only).
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog

from app.application.interfaces.providers import ProviderStatus
from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.interfaces.usage_recorder import (
    DuplicateRequestIdError,
    EffectivePrice,
    NewUsageRecord,
    RecordUsageCommand,
    UsageRecorderPort,
    UsageRecordRow,
    UsageRecordView,
    UsageStatus,
)
from app.application.use_cases.usage.accounting import account, price

_LOGGER = structlog.get_logger(__name__)

DEFAULT_CURRENCY = "USD"

# α7.5 records terminal outcomes only (Q6). ``IN_PROGRESS`` is deliberately absent.
_STATUS_MAP: dict[ProviderStatus, UsageStatus] = {
    ProviderStatus.SUCCEEDED: UsageStatus.SUCCESS,
    ProviderStatus.FAILED: UsageStatus.FAILED,
}


class UsageRecorderService(UsageRecorderPort):
    """Record one terminal provider call as a priced ``usage_records`` row."""

    def __init__(self, *, uow: IUnitOfWork, default_currency: str = DEFAULT_CURRENCY) -> None:
        self._uow = uow
        self._default_currency = default_currency

    async def record(self, command: RecordUsageCommand) -> UsageRecordView:
        # (1) Terminal-only guard (Q6). Reject IN_PROGRESS (and any non-terminal)
        # before touching the DB — usage for async calls is recorded on completion.
        usage_status = _STATUS_MAP.get(command.status)
        if usage_status is None:
            raise ValueError(
                f"UsageRecorder records terminal calls only; got status={command.status!r} "
                "(IN_PROGRESS is recorded later by the completion service under the same request_id)"
            )

        occurred_at = command.occurred_at or datetime.now(UTC)

        # (2) Map ProviderUsage → typed axes + primary billing axis (pure, Q4/D3.4).
        acct = account(command)

        # All persistence access — the read-only pricing lookups AND the append-only
        # insert — happens inside one UoW/transaction (the repos only exist within
        # the ``async with``). No aggregate is touched (W7.5.1).
        async with self._uow:
            # (3) Resolve the effective price per priced unit, then sum Σ(price×qty).
            prices: dict[str, EffectivePrice] = {}
            for unit, _quantity in acct.line_items:
                if unit in prices:
                    continue
                ep = await self._uow.model_pricing.get_effective(
                    model_id=command.model_id, unit=unit, at=occurred_at
                )
                if ep is not None:
                    prices[unit] = ep
            priced = price(acct, prices, default_currency=self._default_currency)

            if priced.unpriced_units:
                # Q5: never fail — warn and price the unconfigured units at 0.
                _LOGGER.warning(
                    "usage.pricing_missing",
                    model_id=str(command.model_id),
                    capability=str(command.capability),
                    units=list(priced.unpriced_units),
                    request_id=command.request_id,
                )

            # (4) Assemble the neutral insert payload. ``render_job_id`` has no column
            # (Q3) → stashed in ``extra`` for traceability alongside the breakdown.
            extra: dict[str, object] = {"pricing_breakdown": list(priced.breakdown)}
            if command.render_job_id is not None:
                extra["render_job_id"] = str(command.render_job_id)

            new = NewUsageRecord(
                tenant_id=command.tenant_id,
                model_id=command.model_id,
                status=usage_status.value,
                unit=acct.unit,
                unit_count=acct.unit_count,
                estimated_cost=priced.estimated_cost,
                currency=priced.currency,
                occurred_at=occurred_at,
                request_id=command.request_id,
                pricing_id=priced.pricing_id,  # type: ignore[arg-type]
                tokens_prompt=acct.tokens_prompt,
                tokens_completion=acct.tokens_completion,
                images_count=acct.images_count,
                seconds_generated=acct.seconds_generated,
                project_id=command.project_id,
                scene_id=command.scene_id,
                prompt_id=command.prompt_id,
                user_id=command.user_id,
                workflow_run_id=command.workflow_run_id,
                workflow_step_id=command.workflow_step_id,
                latency_ms=command.latency_ms,
                error_code=command.error_code,
                extra=extra,
            )

            # (5) Idempotent insert (Q7). On a request_id collision (ADR-0033) recover
            # the pre-existing row and report it as a replay — the call is a no-op.
            try:
                row = await self._uow.usage.insert(new)
                await self._uow.commit()
                replay = False
            except DuplicateRequestIdError:
                assert command.request_id is not None  # only a non-NULL id can collide
                existing = await self._uow.usage.get_by_request_id(command.request_id)
                if existing is None:  # pragma: no cover — the unique just fired
                    raise
                row = existing
                replay = True
                _LOGGER.info(
                    "usage.idempotent_replay",
                    request_id=command.request_id,
                    usage_record_id=str(row.id),
                )

        return _to_view(row, idempotent_replay=replay)


def _to_view(row: UsageRecordRow, *, idempotent_replay: bool) -> UsageRecordView:
    return UsageRecordView(
        id=row.id,
        occurred_at=row.occurred_at,
        unit=row.unit,
        unit_count=row.unit_count,
        estimated_cost=row.estimated_cost,
        currency=row.currency,
        status=row.status,
        idempotent_replay=idempotent_replay,
    )
