"""Unit tests for ``UsageRecorderService`` (Slice α7.5).

Exercises the recorder against in-memory fakes (no DB — the SAVEPOINT idempotency
and effective-at-time pricing SQL are covered by the integration suite). Coverage
map (α7.5 sign-off Q1/Q5/Q6/Q7 + W7.5.1):

* S1 — terminal SUCCEEDED: prices the usage, writes exactly one row, commits once,
  and returns the priced summary (``credits_consumed`` = 0, Q1).
* S2 — IN_PROGRESS is rejected before any write (Q6).
* S3 — FAILED with no usage records a ``failed`` row with unit_count 0 / cost 0.
* S4 — missing pricing → cost 0, pricing_id None, currency default; never raises (Q5).
* S5 — a duplicate request_id returns the pre-existing row as an idempotent replay,
  writing no second row (Q7 / ADR-0033).
* S6 — the recorder is purely observational: the only repository written is
  ``usage`` (W7.5.1); ``render_job_id`` is preserved in ``extra``.

must_pass: S1, S2, S3, S4, S5, S6
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from app.application.interfaces.providers import Capability, ProviderStatus, ProviderUsage
from app.application.interfaces.usage_recorder import PricingUnit, RecordUsageCommand
from app.application.use_cases.usage.usage_recorder_service import UsageRecorderService
from tests.unit.application.use_cases.auth._fakes import (
    FakeModelPricingRepository,
    FakeUnitOfWork,
    FakeUsageRecordRepository,
)

_AT = datetime(2026, 6, 1, tzinfo=UTC)


def _service(
    *,
    usage: FakeUsageRecordRepository | None = None,
    model_pricing: FakeModelPricingRepository | None = None,
) -> tuple[UsageRecorderService, FakeUnitOfWork]:
    uow = FakeUnitOfWork(
        usage=usage or FakeUsageRecordRepository(),
        model_pricing=model_pricing or FakeModelPricingRepository(),
    )
    return UsageRecorderService(uow=uow), uow


@pytest.mark.unit
async def test_s1_terminal_success_prices_and_records() -> None:
    model_id = uuid4()
    pricing = FakeModelPricingRepository()
    pricing.set_price(
        model_id=model_id, unit=PricingUnit.COMPLETION_TOKEN.value, price_per_unit="0.00003"
    )
    service, uow = _service(model_pricing=pricing)

    view = await service.record(
        RecordUsageCommand(
            tenant_id=uuid4(),
            model_id=model_id,
            status=ProviderStatus.SUCCEEDED,
            request_id="req-1",
            capability=Capability.LLM,
            usage=ProviderUsage(unit="tokens", quantity=1000),
            occurred_at=_AT,
        )
    )

    assert view.status == "success"
    assert view.unit == PricingUnit.COMPLETION_TOKEN.value
    assert view.unit_count == Decimal(1000)
    assert view.estimated_cost == Decimal("0.03")  # 1000 * 0.00003
    assert view.currency == "USD"
    assert view.idempotent_replay is False
    assert uow.commits == 1
    assert len(uow._fake_usage.inserted) == 1
    assert uow._fake_usage.inserted[0].credits_consumed == Decimal(0)  # Q1


@pytest.mark.unit
async def test_s2_in_progress_rejected_without_write() -> None:
    service, uow = _service()

    with pytest.raises(ValueError, match="terminal"):
        await service.record(
            RecordUsageCommand(
                tenant_id=uuid4(),
                model_id=uuid4(),
                status=ProviderStatus.IN_PROGRESS,
                capability=Capability.VIDEO,
                usage=ProviderUsage(unit="seconds", quantity=5),
            )
        )

    assert uow._fake_usage.inserted == []
    assert uow.commits == 0


@pytest.mark.unit
async def test_s3_failed_without_usage_records_zero() -> None:
    service, uow = _service()

    view = await service.record(
        RecordUsageCommand(
            tenant_id=uuid4(),
            model_id=uuid4(),
            status=ProviderStatus.FAILED,
            request_id="req-fail",
            capability=Capability.IMAGE,
            usage=None,
            occurred_at=_AT,
            error_code="provider_error",
        )
    )

    assert view.status == "failed"
    assert view.unit == PricingUnit.IMAGE.value
    assert view.unit_count == Decimal(0)
    assert view.estimated_cost == Decimal(0)
    assert len(uow._fake_usage.inserted) == 1
    assert uow._fake_usage.inserted[0].error_code == "provider_error"


@pytest.mark.unit
async def test_s4_missing_pricing_never_raises_and_costs_zero() -> None:
    service, uow = _service()  # no prices configured

    view = await service.record(
        RecordUsageCommand(
            tenant_id=uuid4(),
            model_id=uuid4(),
            status=ProviderStatus.SUCCEEDED,
            request_id="req-nopx",
            capability=Capability.IMAGE,
            usage=ProviderUsage(unit="image", quantity=2),
            occurred_at=_AT,
        )
    )

    assert view.estimated_cost == Decimal(0)
    assert view.currency == "USD"
    assert uow._fake_usage.inserted[0].pricing_id is None


@pytest.mark.unit
async def test_s5_duplicate_request_id_is_idempotent_replay() -> None:
    model_id = uuid4()
    tenant_id = uuid4()
    usage_repo = FakeUsageRecordRepository()
    pricing = FakeModelPricingRepository()
    pricing.set_price(model_id=model_id, unit=PricingUnit.IMAGE.value, price_per_unit="0.02")

    def _cmd() -> RecordUsageCommand:
        return RecordUsageCommand(
            tenant_id=tenant_id,
            model_id=model_id,
            status=ProviderStatus.SUCCEEDED,
            request_id="dup-req",
            capability=Capability.IMAGE,
            usage=ProviderUsage(unit="image", quantity=1),
            occurred_at=_AT,
        )

    service1 = UsageRecorderService(uow=FakeUnitOfWork(usage=usage_repo, model_pricing=pricing))
    first = await service1.record(_cmd())

    service2 = UsageRecorderService(uow=FakeUnitOfWork(usage=usage_repo, model_pricing=pricing))
    second = await service2.record(_cmd())

    assert first.idempotent_replay is False
    assert second.idempotent_replay is True
    assert second.id == first.id
    assert len(usage_repo.inserted) == 1  # no second row written


@pytest.mark.unit
async def test_s6_observational_only_writes_usage_and_preserves_render_job_id() -> None:
    model_id = uuid4()
    render_job_id = uuid4()
    service, uow = _service()

    await service.record(
        RecordUsageCommand(
            tenant_id=uuid4(),
            model_id=model_id,
            status=ProviderStatus.SUCCEEDED,
            request_id="req-obs",
            capability=Capability.VIDEO,
            usage=ProviderUsage(unit="seconds", quantity=4),
            occurred_at=_AT,
            render_job_id=render_job_id,
        )
    )

    # Only the usage repo was written (W7.5.1) — no aggregate touched.
    assert len(uow._fake_usage.inserted) == 1
    assert uow._fake_render_jobs._jobs == {}
    assert uow._fake_workflow_runs._runs == {}
    assert uow._fake_outbox.events == []
    assert uow._fake_media._media == {}
    # render_job_id has no column → preserved in extra for traceability (Q3).
    assert uow._fake_usage.inserted[0].extra["render_job_id"] == str(render_job_id)
