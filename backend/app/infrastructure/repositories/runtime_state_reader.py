"""α8.5e.4 — runtime-state reader (read-only, async).

Materialises a :class:`RuntimeSnapshot` from the α8.5e operational tables (migration
0011): ``provider_health`` (observational), ``provider_quota_state`` (operational),
``adapter_runtime_metrics`` (historical). Read-only — the resolver never mutates these
(W8.5e.3); the Execution Runtime / Health Worker own the writes.

Row → value-object mapping lives in pure module-level functions (unit-tested without a
DB); ``load_snapshot`` only adds the async query plumbing. Empty tables yield an empty
snapshot (all providers/adapters then score at neutral health / unlimited quota).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import TextClause

from app.application.interfaces.runtime_state_reader import IRuntimeStateReader
from app.domain.resolver.models import (
    AdapterMetrics,
    ProviderHealth,
    QuotaState,
    RuntimeSnapshot,
)

_HEALTH_SQL = text("SELECT provider_id, health_score, error_rate FROM provider_health")
_QUOTA_SQL = text(
    "SELECT provider_id, window, remaining FROM provider_quota_state "
    "ORDER BY provider_id, window"
)
_METRICS_SQL = text("SELECT adapter_id, avg_latency_ms, success_rate FROM adapter_runtime_metrics")


# --------------------------------------------------------------------------- #
# Pure row → value-object mapping (DB-free; unit-tested directly)
# --------------------------------------------------------------------------- #
def health_from_row(row: Mapping[str, Any]) -> ProviderHealth:
    return ProviderHealth(
        provider_id=row["provider_id"],
        health_score=float(row["health_score"]),
        error_rate=float(row["error_rate"]),
    )


def quota_from_row(row: Mapping[str, Any]) -> QuotaState:
    remaining = row["remaining"]
    return QuotaState(
        provider_id=row["provider_id"],
        window=row["window"],
        remaining=int(remaining) if remaining is not None else None,
    )


def metrics_from_row(row: Mapping[str, Any]) -> AdapterMetrics:
    latency = row["avg_latency_ms"]
    success = row["success_rate"]
    return AdapterMetrics(
        adapter_id=row["adapter_id"],
        avg_latency_ms=int(latency) if latency is not None else None,
        success_rate=float(success) if success is not None else None,
    )


def group_quota(rows: Iterable[Mapping[str, Any]]) -> dict[str, tuple[QuotaState, ...]]:
    """Group quota windows (already ordered) by provider."""
    grouped: dict[str, list[QuotaState]] = {}
    for row in rows:
        state = quota_from_row(row)
        grouped.setdefault(state.provider_id, []).append(state)
    return {pid: tuple(states) for pid, states in grouped.items()}


def build_runtime_snapshot(
    *,
    health_rows: Sequence[Mapping[str, Any]],
    quota_rows: Sequence[Mapping[str, Any]],
    metrics_rows: Sequence[Mapping[str, Any]],
) -> RuntimeSnapshot:
    return RuntimeSnapshot(
        health={r["provider_id"]: health_from_row(r) for r in health_rows},
        quota=group_quota(quota_rows),
        metrics={r["adapter_id"]: metrics_from_row(r) for r in metrics_rows},
    )


class RuntimeStateReader(IRuntimeStateReader):
    """Async read-only operational-state accessor over an :class:`AsyncSession`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load_snapshot(self) -> RuntimeSnapshot:
        return build_runtime_snapshot(
            health_rows=await self._rows(_HEALTH_SQL),
            quota_rows=await self._rows(_QUOTA_SQL),
            metrics_rows=await self._rows(_METRICS_SQL),
        )

    async def _rows(self, sql: TextClause) -> list[dict[str, Any]]:
        result = (await self._session.execute(sql)).mappings().all()
        return [dict(row) for row in result]
