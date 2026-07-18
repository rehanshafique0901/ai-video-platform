"""Unit tests for the pure usage accounting/pricing policy (Slice α7.5).

Coverage map (α7.5 sign-off D3.4 / Q4 / Q5):

* A1 — LLM with an explicit prompt+completion split → both typed token axes, both
  priced line items, primary axis = completion_token.
* A2 — LLM minimal α7.4 mock (single ``quantity``, no detail) → treated as
  completion tokens; prompt axis stays None.
* A3 — IMAGE → images_count axis + image line item + primary unit=image.
* A4 — VIDEO → seconds_generated axis + video_second line item.
* A5 — VOICE → seconds_generated axis + audio_second line item (differs from VIDEO
  only in the pricing unit).
* A6 — terminal call with no usage → primary unit, unit_count 0, no line items.
* A7 — capability is required (None → ValueError).
* P1 — price sums Σ(unit_price × quantity) across multiple units.
* P2 — a missing price contributes 0 and is reported in unpriced_units.
* P3 — all pricing missing → cost 0, pricing_id None, currency = default.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from app.application.interfaces.providers import Capability, ProviderStatus, ProviderUsage
from app.application.interfaces.usage_recorder import (
    EffectivePrice,
    PricingUnit,
    RecordUsageCommand,
)
from app.application.use_cases.usage.accounting import account, price

_AT = datetime(2026, 6, 1, tzinfo=UTC)


def _cmd(capability: Capability, usage: ProviderUsage | None) -> RecordUsageCommand:
    return RecordUsageCommand(
        tenant_id=uuid4(),
        model_id=uuid4(),
        status=ProviderStatus.SUCCEEDED,
        capability=capability,
        usage=usage,
        occurred_at=_AT,
    )


def _price(unit: str, per_unit: str, currency: str = "USD") -> EffectivePrice:
    return EffectivePrice(
        pricing_id=uuid4(), unit=unit, price_per_unit=Decimal(per_unit), currency=currency
    )


@pytest.mark.unit
def test_a1_llm_explicit_token_split() -> None:
    usage = ProviderUsage(
        unit="tokens",
        quantity=150,
        detail={"tokens_prompt": 100, "tokens_completion": 50},
    )
    acct = account(_cmd(Capability.LLM, usage))

    assert acct.unit == PricingUnit.COMPLETION_TOKEN.value
    assert acct.unit_count == Decimal(50)
    assert acct.tokens_prompt == 100
    assert acct.tokens_completion == 50
    assert acct.line_items == (
        (PricingUnit.PROMPT_TOKEN.value, Decimal(100)),
        (PricingUnit.COMPLETION_TOKEN.value, Decimal(50)),
    )


@pytest.mark.unit
def test_a2_llm_minimal_single_quantity_is_completion() -> None:
    acct = account(_cmd(Capability.LLM, ProviderUsage(unit="tokens", quantity=42)))

    assert acct.tokens_prompt is None
    assert acct.tokens_completion == 42
    assert acct.unit == PricingUnit.COMPLETION_TOKEN.value
    assert acct.unit_count == Decimal(42)
    assert acct.line_items == ((PricingUnit.COMPLETION_TOKEN.value, Decimal(42)),)


@pytest.mark.unit
def test_a3_image_counts() -> None:
    acct = account(_cmd(Capability.IMAGE, ProviderUsage(unit="image", quantity=3)))

    assert acct.images_count == 3
    assert acct.unit == PricingUnit.IMAGE.value
    assert acct.unit_count == Decimal(3)
    assert acct.line_items == ((PricingUnit.IMAGE.value, Decimal(3)),)


@pytest.mark.unit
def test_a4_video_seconds() -> None:
    usage = ProviderUsage(unit="seconds", quantity=8, detail={"seconds_generated": 7.5})
    acct = account(_cmd(Capability.VIDEO, usage))

    assert acct.seconds_generated == Decimal("7.5")
    assert acct.unit == PricingUnit.VIDEO_SECOND.value
    assert acct.unit_count == Decimal("7.5")
    assert acct.line_items == ((PricingUnit.VIDEO_SECOND.value, Decimal("7.5")),)


@pytest.mark.unit
def test_a5_voice_seconds_uses_audio_second() -> None:
    acct = account(_cmd(Capability.VOICE, ProviderUsage(unit="seconds", quantity=12)))

    assert acct.unit == PricingUnit.AUDIO_SECOND.value
    assert acct.unit_count == Decimal(12)
    assert acct.line_items == ((PricingUnit.AUDIO_SECOND.value, Decimal(12)),)


@pytest.mark.unit
def test_a6_no_usage_yields_zero_primary_no_line_items() -> None:
    acct = account(_cmd(Capability.IMAGE, None))

    assert acct.unit == PricingUnit.IMAGE.value
    assert acct.unit_count == Decimal(0)
    assert acct.images_count == 0
    assert acct.line_items == ()


@pytest.mark.unit
def test_a7_capability_required() -> None:
    cmd = RecordUsageCommand(
        tenant_id=uuid4(),
        model_id=uuid4(),
        status=ProviderStatus.SUCCEEDED,
        capability=None,
        usage=ProviderUsage(unit="tokens", quantity=1),
    )
    with pytest.raises(ValueError, match="capability is required"):
        account(cmd)


@pytest.mark.unit
def test_p1_sums_multi_unit_cost() -> None:
    usage = ProviderUsage(
        unit="tokens", quantity=0, detail={"tokens_prompt": 1000, "tokens_completion": 500}
    )
    acct = account(_cmd(Capability.LLM, usage))
    prices = {
        PricingUnit.PROMPT_TOKEN.value: _price(PricingUnit.PROMPT_TOKEN.value, "0.00001"),
        PricingUnit.COMPLETION_TOKEN.value: _price(PricingUnit.COMPLETION_TOKEN.value, "0.00003"),
    }

    priced = price(acct, prices, default_currency="USD")

    # 1000 * 0.00001 + 500 * 0.00003 = 0.01 + 0.015 = 0.025
    assert priced.estimated_cost == Decimal("0.025")
    assert priced.currency == "USD"
    assert priced.pricing_id == prices[PricingUnit.COMPLETION_TOKEN.value].pricing_id
    assert priced.unpriced_units == ()


@pytest.mark.unit
def test_p2_missing_price_contributes_zero_and_is_reported() -> None:
    usage = ProviderUsage(
        unit="tokens", quantity=0, detail={"tokens_prompt": 1000, "tokens_completion": 500}
    )
    acct = account(_cmd(Capability.LLM, usage))
    # Only completion is priced; prompt is unconfigured.
    prices = {
        PricingUnit.COMPLETION_TOKEN.value: _price(PricingUnit.COMPLETION_TOKEN.value, "0.00003")
    }

    priced = price(acct, prices, default_currency="USD")

    assert priced.estimated_cost == Decimal("0.015")  # completion only
    assert priced.unpriced_units == (PricingUnit.PROMPT_TOKEN.value,)
    assert priced.pricing_id == prices[PricingUnit.COMPLETION_TOKEN.value].pricing_id


@pytest.mark.unit
def test_p3_all_pricing_missing_defaults_currency_and_null_pricing_id() -> None:
    acct = account(_cmd(Capability.IMAGE, ProviderUsage(unit="image", quantity=2)))

    priced = price(acct, {}, default_currency="USD")

    assert priced.estimated_cost == Decimal(0)
    assert priced.pricing_id is None
    assert priced.currency == "USD"
    assert priced.unpriced_units == (PricingUnit.IMAGE.value,)
