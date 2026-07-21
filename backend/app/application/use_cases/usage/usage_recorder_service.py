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

The account→price→idempotent-insert body is factored into
:func:`record_usage_in_uow`, a **transaction-participating** helper that runs on an
**already-open** UoW and **does not commit** — so the α7.6 runner can record usage
inside its own single transaction (α7.6 sign-off Q5). :meth:`UsageRecorderService.record`
is the standalone entrypoint: it opens the UoW, calls the helper, and commits.
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


async def record_usage_in_uow(
    uow: IUnitOfWork,
    command: RecordUsageCommand,
    *,
    default_currency: str = DEFAULT_CURRENCY,
) -> UsageRecordView:
    """Account → price → idempotent-insert one usage row on an **already-open** UoW.

    The transaction-participating core of the recorder (α7.6 sign-off Q5): it uses
    ``uow.model_pricing`` (read) + ``uow.usage`` (append) but **does not open the
    UoW and does not commit** — the caller owns the transaction. This lets the α7.6
    runner record usage inside its single run transaction (so a replay cannot
    double-charge and a crash before the runner's commit rolls the row back with the
    rest of the step). :meth:`UsageRecorderService.record` wraps this for standalone
    callers. Enforces the same invariants as ``record``: terminal-only (Q6),
    idempotent on ``request_id`` (Q7), missing pricing never blocks (Q5), purely
    observational (W7.5.1).
    """
    # (1) Terminal-only guard (Q6). Reject IN_PROGRESS (and any non-terminal) before
    # touching the DB — usage for async calls is recorded on completion (α8.3).
    usage_status = _STATUS_MAP.get(command.status)
    if usage_status is None:
        raise ValueError(
            f"UsageRecorder records terminal calls only; got status={command.status!r} "
            "(IN_PROGRESS is recorded later by the completion service under the same request_id)"
        )

    occurred_at = command.occurred_at or datetime.now(UTC)

    # (2) Map ProviderUsage → typed axes + primary billing axis (pure, Q4/D3.4).
    acct = account(command)

    # (3) Resolve the effective price per priced unit, then sum Σ(price×qty).
    prices: dict[str, EffectivePrice] = {}
    for unit, _quantity in acct.line_items:
        if unit in prices:
            continue
        ep = await uow.model_pricing.get_effective(
            model_id=command.model_id, unit=unit, at=occurred_at
        )
        if ep is not None:
            prices[unit] = ep
    priced = price(acct, prices, default_currency=default_currency)

    if priced.unpriced_units:
        # Q5: never fail — warn and price the unconfigured units at 0.
        _LOGGER.warning(
            "usage.pricing_missing",
            model_id=str(command.model_id),
            capability=str(command.capability),
            units=list(priced.unpriced_units),
            request_id=command.request_id,
        )

    # (4) Assemble the neutral insert payload. ``render_job_id`` has no column (Q3)
    # → stashed in ``extra`` for traceability alongside the breakdown.
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

    # (5) Idempotent insert (Q7). On a request_id collision (ADR-0033) recover the
    # pre-existing row and report it as a replay — the call is a no-op. The insert is
    # SAVEPOINT-guarded (``begin_nested``), so the collision rolls back only the
    # failed insert and the caller's transaction survives for the recovery SELECT.
    try:
        row = await uow.usage.insert(new)
        replay = False
    except DuplicateRequestIdError:
        assert command.request_id is not None  # only a non-NULL id can collide
        existing = await uow.usage.get_by_request_id(command.request_id)
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


class UsageRecorderService(UsageRecorderPort):
    """Record one terminal provider call as a priced ``usage_records`` row."""

    def __init__(self, *, uow: IUnitOfWork, default_currency: str = DEFAULT_CURRENCY) -> None:
        self._uow = uow
        self._default_currency = default_currency

    async def record(self, command: RecordUsageCommand) -> UsageRecordView:
        # Standalone unit of work: open → record (helper) → commit. The α7.6 runner
        # instead calls ``record_usage_in_uow`` on its own open transaction (Q5).
        async with self._uow:
            view = await record_usage_in_uow(
                self._uow, command, default_currency=self._default_currency
            )
            await self._uow.commit()
        return view


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
