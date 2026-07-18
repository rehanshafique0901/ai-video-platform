"""Pure cost/accounting policy for the Usage Recorder (Slice α7.5).

Two side-effect-free steps the :class:`UsageRecorderService` composes:

1. :func:`account` — map an α7.4 :class:`ProviderUsage` onto the typed
   ``usage_records`` axes (``tokens_prompt`` / ``tokens_completion`` /
   ``images_count`` / ``seconds_generated``) **by capability** (α7.5 sign-off
   D3.4), and derive the **primary billing axis** (``unit`` / ``unit_count``) plus
   the set of priced line items.
2. :func:`price` — sum ``Σ(unit_price × quantity)`` over the line items using the
   prices the service resolved (Q4). Missing pricing contributes 0 (Q5).

Kept free of I/O and ORM so it is exhaustively unit-testable with a dict of
prices — the service does the (async) pricing lookups and DB writes.

Axis extraction is tolerant of the minimal α7.4 mock usage (a single
``ProviderUsage(unit=..., quantity=n)`` with no ``detail``) **and** of a richer
``detail`` carrying an explicit token/second breakdown, so real providers in α8.x
need no recorder change.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.application.interfaces.providers import Capability
from app.application.interfaces.usage_recorder import (
    EffectivePrice,
    PricingUnit,
    RecordUsageCommand,
)

# Per-capability primary billing axis (α7.5 sign-off Q4 / D3.4). The single
# ``usage_records.unit`` records this axis; the granular breakdown is priced via
# ``line_items`` and stored in the typed columns + ``extra``.
_PRIMARY_UNIT: dict[Capability, PricingUnit] = {
    Capability.LLM: PricingUnit.COMPLETION_TOKEN,
    Capability.IMAGE: PricingUnit.IMAGE,
    Capability.VIDEO: PricingUnit.VIDEO_SECOND,
    Capability.VOICE: PricingUnit.AUDIO_SECOND,
}


@dataclass(frozen=True, slots=True)
class UsageAccounting:
    """Typed usage axes + the primary billing axis + the priced line items."""

    unit: str
    unit_count: Decimal
    tokens_prompt: int | None
    tokens_completion: int | None
    images_count: int | None
    seconds_generated: Decimal | None
    line_items: tuple[tuple[str, Decimal], ...]


@dataclass(frozen=True, slots=True)
class PricedUsage:
    """Result of pricing: total cost, its currency, the primary pricing row, breakdown."""

    estimated_cost: Decimal
    currency: str
    pricing_id: object | None  # UUID | None (kept ``object`` to avoid a uuid import here)
    breakdown: tuple[dict[str, Any], ...]
    unpriced_units: tuple[str, ...]


def _to_decimal(value: Any) -> Decimal:
    """Coerce an int/float/str/Decimal quantity to ``Decimal`` without float noise."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _opt_int(detail: Mapping[str, Any], *keys: str) -> int | None:
    for key in keys:
        if key in detail and detail[key] is not None:
            return int(detail[key])
    return None


def _opt_num(detail: Mapping[str, Any], *keys: str) -> Decimal | None:
    for key in keys:
        if key in detail and detail[key] is not None:
            return _to_decimal(detail[key])
    return None


def account(command: RecordUsageCommand) -> UsageAccounting:
    """Map ``command.usage`` onto the typed axes + primary axis, keyed by capability.

    ``capability`` is required (the primary ``unit`` cannot be chosen without it).
    A terminal call with no usage (e.g. a ``FAILED`` call that produced nothing) is
    valid: it yields the capability's primary unit with ``unit_count = 0``, no
    typed axes, and no line items (cost 0).
    """
    capability = command.capability
    if capability is None:
        raise ValueError("RecordUsageCommand.capability is required to account for usage")
    primary = _PRIMARY_UNIT.get(capability)
    if primary is None:
        raise ValueError(f"no usage accounting policy for capability {capability!r}")

    usage = command.usage
    quantity: Decimal = _to_decimal(usage.quantity) if usage is not None else Decimal(0)
    detail: Mapping[str, Any] = dict(usage.detail) if usage is not None else {}

    tokens_prompt: int | None = None
    tokens_completion: int | None = None
    images_count: int | None = None
    seconds_generated: Decimal | None = None
    line_items: list[tuple[str, Decimal]] = []

    if capability is Capability.LLM:
        tokens_prompt = _opt_int(detail, "tokens_prompt", "prompt_tokens")
        tokens_completion = _opt_int(detail, "tokens_completion", "completion_tokens")
        # Minimal α7.4 mock: a single ``quantity`` with no split → treat it as
        # completion tokens (the primary axis) so the row is still priced.
        if tokens_prompt is None and tokens_completion is None:
            tokens_completion = int(quantity)
        if tokens_prompt:
            line_items.append((PricingUnit.PROMPT_TOKEN.value, _to_decimal(tokens_prompt)))
        if tokens_completion:
            line_items.append((PricingUnit.COMPLETION_TOKEN.value, _to_decimal(tokens_completion)))
        unit_count = _to_decimal(tokens_completion or 0)
    elif capability is Capability.IMAGE:
        images_count = _opt_int(detail, "images_count", "images")
        if images_count is None:
            images_count = int(quantity)
        if images_count:
            line_items.append((PricingUnit.IMAGE.value, _to_decimal(images_count)))
        unit_count = _to_decimal(images_count)
    else:  # VIDEO / VOICE — both meter on seconds, differing only in pricing unit.
        seconds_generated = _opt_num(detail, "seconds_generated", "seconds")
        if seconds_generated is None:
            seconds_generated = quantity
        if seconds_generated:
            line_items.append((primary.value, seconds_generated))
        unit_count = seconds_generated

    return UsageAccounting(
        unit=primary.value,
        unit_count=unit_count,
        tokens_prompt=tokens_prompt,
        tokens_completion=tokens_completion,
        images_count=images_count,
        seconds_generated=seconds_generated,
        line_items=tuple(line_items),
    )


def price(
    accounting: UsageAccounting,
    prices: Mapping[str, EffectivePrice],
    *,
    default_currency: str,
) -> PricedUsage:
    """Sum ``Σ(unit_price × quantity)`` over the line items (Q4).

    A line item with no resolved price contributes 0 and is reported in
    ``unpriced_units`` (the service logs a WARN — Q5). ``pricing_id`` is the
    primary unit's pricing row when priced, else ``None``; ``currency`` prefers the
    primary unit's, then any priced line's, else ``default_currency``.
    """
    total = Decimal(0)
    breakdown: list[dict[str, Any]] = []
    unpriced: list[str] = []
    for unit, quantity in accounting.line_items:
        ep = prices.get(unit)
        if ep is None:
            unpriced.append(unit)
            breakdown.append({"unit": unit, "quantity": str(quantity), "priced": False})
            continue
        line_cost = ep.price_per_unit * quantity
        total += line_cost
        breakdown.append(
            {
                "unit": unit,
                "quantity": str(quantity),
                "unit_price": str(ep.price_per_unit),
                "line_cost": str(line_cost),
                "currency": ep.currency,
                "priced": True,
            }
        )

    primary_price = prices.get(accounting.unit)
    if primary_price is not None:
        currency = primary_price.currency
        pricing_id: object | None = primary_price.pricing_id
    else:
        priced_currency = next(
            (prices[u].currency for u, _ in accounting.line_items if u in prices), None
        )
        currency = priced_currency or default_currency
        pricing_id = None

    return PricedUsage(
        estimated_cost=total,
        currency=currency,
        pricing_id=pricing_id,
        breakdown=tuple(breakdown),
        unpriced_units=tuple(unpriced),
    )
