"""α8.5e.5 — pure candidate-list serialiser for the resolution ledger (no DB)."""

from __future__ import annotations

import pytest

from app.domain.resolver import ResolveRequest, resolve
from app.domain.resolver.models import (
    AdapterInfo,
    CatalogueSnapshot,
    ExecutableAdapters,
    ExecutionMode,
    Pricing,
    ProviderInfo,
    RoutingStrategy,
    RuntimeSnapshot,
)
from app.infrastructure.repositories.resolution_ledger_writer import candidate_list_payload

pytestmark = pytest.mark.unit


def _resolution() -> object:
    catalogue = CatalogueSnapshot(
        catalogue_version="2026.07",
        manifest_digest="d",
        providers={
            "p": ProviderInfo(
                id="p",
                pricing=Pricing.FREE,
                commercial=True,
                score_quality=70,
                score_cost=100,
                score_speed=80,
                score_reliability=75,
            )
        },
        adapters=(
            AdapterInfo(
                id="p.local",
                provider_id="p",
                capability_id="cap",
                execution_mode=ExecutionMode.LOCAL,
            ),
            AdapterInfo(
                id="p.cloud",
                provider_id="p",
                capability_id="cap",
                execution_mode=ExecutionMode.CLOUD,
            ),
        ),
        routing={"default": RoutingStrategy.BALANCED},
    )
    # local_only makes the cloud adapter ineligible → exercises both branches.
    return resolve(
        ResolveRequest(capability="cap", local_only=True),
        catalogue,
        RuntimeSnapshot(),
        ExecutableAdapters(frozenset({"p.local", "p.cloud"})),
    )


def test_candidate_list_payload_captures_full_ranked_list() -> None:
    payload = candidate_list_payload(_resolution())  # type: ignore[arg-type]
    by_id = {c["adapter_id"]: c for c in payload}
    assert set(by_id) == {"p.local", "p.cloud"}  # winners AND filtered

    eligible = by_id["p.local"]
    assert eligible["eligible"] is True
    assert eligible["ineligible_reason"] is None
    assert eligible["breakdown"]["score_schema"] == 1
    assert set(eligible["breakdown"]["components"]) == {
        "quality",
        "cost",
        "speed",
        "reliability",
        "hardware",
    }

    filtered = by_id["p.cloud"]
    assert filtered["eligible"] is False
    assert filtered["ineligible_reason"] == "not_local"
    assert filtered["breakdown"] is None
