"""α8.5e.3 — catalogue reader (read-only, async).

Materialises a :class:`CatalogueSnapshot` from the α8.5d catalogue tables (migration
0010). The catalogue tables have no ORM models (they are seeded by
``scripts/seed_providers.py`` and only *read* at runtime), so this uses async Core
``text()`` and maps ``RowMapping`` dicts into pure domain value objects.

Row → value-object mapping lives in module-level pure functions so it can be unit-tested
without a database; ``load_snapshot`` only adds the async query plumbing. Every row is
loaded (including ``enabled = false``): the resolver reports disabled entries as
ineligible-with-reason rather than hiding them (§4.1).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import TextClause

from app.application.interfaces.catalogue_reader import ICatalogueReader
from app.domain.resolver.models import (
    AdapterInfo,
    CatalogueSnapshot,
    DeviceProfile,
    ExecutionMode,
    Pricing,
    ProviderInfo,
    RoutingStrategy,
)

_META_SQL = text(
    "SELECT catalogue_version, manifest_digest FROM provider_registry_meta WHERE id IS TRUE"
)
_PROVIDERS_SQL = text(
    "SELECT id, pricing, commercial, score_quality, score_cost, score_speed, "
    "score_reliability, enabled FROM providers"
)
_ADAPTERS_SQL = text(
    "SELECT id, provider_id, capability_id, execution_mode, enabled, implemented, "
    "cost_amount, supports, runtime FROM provider_adapters"
)
_FALLBACKS_SQL = text(
    "SELECT adapter_id, fallback_adapter_id FROM adapter_fallbacks ORDER BY adapter_id, ordinal"
)
_ROUTING_SQL = text("SELECT scope, strategy FROM routing_policies")
_DEVICES_SQL = text("SELECT id, ram_gb, backend FROM device_profiles")


# --------------------------------------------------------------------------- #
# Pure row → value-object mapping (DB-free; unit-tested directly)
# --------------------------------------------------------------------------- #
def provider_from_row(row: Mapping[str, Any]) -> ProviderInfo:
    return ProviderInfo(
        id=row["id"],
        pricing=Pricing(row["pricing"]),
        commercial=bool(row["commercial"]),
        score_quality=int(row["score_quality"]),
        score_cost=int(row["score_cost"]),
        score_speed=int(row["score_speed"]),
        score_reliability=int(row["score_reliability"]),
        enabled=bool(row["enabled"]),
    )


def adapter_from_row(row: Mapping[str, Any], fallbacks: tuple[str, ...]) -> AdapterInfo:
    runtime = row["runtime"] if isinstance(row["runtime"], dict) else {}
    hardware = runtime.get("hardware", {}) if isinstance(runtime.get("hardware"), dict) else {}
    supports = row["supports"] if isinstance(row["supports"], dict) else {}
    mode = row["execution_mode"]
    cost = row["cost_amount"]
    return AdapterInfo(
        id=row["id"],
        provider_id=row["provider_id"],
        capability_id=row["capability_id"],
        execution_mode=ExecutionMode(mode) if mode is not None else None,
        enabled=bool(row["enabled"]),
        implemented=bool(row["implemented"]),
        min_ram_gb=hardware.get("minimum_ram_gb"),
        recommended_ram_gb=hardware.get("recommended_ram_gb"),
        cost_amount=float(cost) if cost is not None else None,
        supports_commercial=supports.get("commercial"),
        fallbacks=fallbacks,
    )


def device_from_row(row: Mapping[str, Any]) -> DeviceProfile:
    return DeviceProfile(id=row["id"], ram_gb=row["ram_gb"], backend=row["backend"])


def group_fallbacks(rows: Iterable[Mapping[str, Any]]) -> dict[str, tuple[str, ...]]:
    """Group ordered fallback edges (already sorted by ordinal) by adapter."""
    grouped: dict[str, list[str]] = {}
    for row in rows:
        grouped.setdefault(row["adapter_id"], []).append(row["fallback_adapter_id"])
    return {aid: tuple(fbs) for aid, fbs in grouped.items()}


def build_snapshot(
    *,
    catalogue_version: str,
    manifest_digest: str,
    provider_rows: Sequence[Mapping[str, Any]],
    adapter_rows: Sequence[Mapping[str, Any]],
    fallback_rows: Sequence[Mapping[str, Any]],
    routing_rows: Sequence[Mapping[str, Any]],
    device_rows: Sequence[Mapping[str, Any]],
) -> CatalogueSnapshot:
    fallbacks = group_fallbacks(fallback_rows)
    return CatalogueSnapshot(
        catalogue_version=catalogue_version,
        manifest_digest=manifest_digest,
        providers={r["id"]: provider_from_row(r) for r in provider_rows},
        adapters=tuple(adapter_from_row(r, fallbacks.get(r["id"], ())) for r in adapter_rows),
        routing={r["scope"]: RoutingStrategy(r["strategy"]) for r in routing_rows},
        devices={r["id"]: device_from_row(r) for r in device_rows},
    )


class CatalogueReader(ICatalogueReader):
    """Async read-only catalogue accessor over an :class:`AsyncSession`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load_snapshot(self) -> CatalogueSnapshot | None:
        meta = (await self._session.execute(_META_SQL)).mappings().first()
        if meta is None:
            return None
        return build_snapshot(
            catalogue_version=meta["catalogue_version"],
            manifest_digest=meta["manifest_digest"],
            provider_rows=await self._rows(_PROVIDERS_SQL),
            adapter_rows=await self._rows(_ADAPTERS_SQL),
            fallback_rows=await self._rows(_FALLBACKS_SQL),
            routing_rows=await self._rows(_ROUTING_SQL),
            device_rows=await self._rows(_DEVICES_SQL),
        )

    async def _rows(self, sql: TextClause) -> list[dict[str, Any]]:
        result = (await self._session.execute(sql)).mappings().all()
        return [dict(row) for row in result]
