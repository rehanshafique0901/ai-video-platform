"""α8.5e.5 — resolution ledger writer (async).

Persists a :class:`Resolution` into ``generation_resolution_ledger`` (migration 0011) for
provenance / replay. The candidate-list serialiser is a pure module-level function
(unit-tested without a DB); the writer only adds the async INSERT plumbing.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.interfaces.resolution_ledger import ExecutionOutcome, IResolutionLedger
from app.domain.resolver import Resolution

_INSERT_SQL = text(
    """
    INSERT INTO generation_resolution_ledger
        (generation_id, capability, catalogue_version, manifest_digest, resolver_version,
         routing_strategy, candidate_list, chosen_adapter, start_time, end_time, execution_result)
    VALUES
        (CAST(:generation_id AS uuid), :capability, :catalogue_version, :manifest_digest,
         :resolver_version, CAST(:routing_strategy AS routing_strategy),
         CAST(:candidate_list AS jsonb), :chosen_adapter, :start_time, :end_time,
         CAST(:execution_result AS execution_result))
    RETURNING id
    """
)


def candidate_list_payload(resolution: Resolution) -> list[dict[str, Any]]:
    """Serialise the full ranked candidate list (W8.5e.5): winners *and* filtered ones."""
    payload: list[dict[str, Any]] = []
    for candidate in resolution.candidates:
        payload.append(
            {
                "adapter_id": candidate.adapter_id,
                "provider_id": candidate.provider_id,
                "score": candidate.score,
                "eligible": candidate.eligible,
                "ineligible_reason": candidate.ineligible_reason,
                "breakdown": candidate.breakdown.as_dict() if candidate.breakdown else None,
            }
        )
    return payload


class ResolutionLedgerWriter(IResolutionLedger):
    """Append resolution rows to ``generation_resolution_ledger``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        *,
        generation_id: UUID,
        resolution: Resolution,
        chosen_adapter: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        execution_result: ExecutionOutcome | None = None,
    ) -> UUID:
        params = {
            "generation_id": str(generation_id),
            "capability": resolution.capability,
            "catalogue_version": resolution.catalogue_version,
            "manifest_digest": resolution.manifest_digest,
            "resolver_version": resolution.resolver_version,
            "routing_strategy": resolution.routing_strategy.value,
            "candidate_list": json.dumps(candidate_list_payload(resolution)),
            "chosen_adapter": chosen_adapter,
            "start_time": start_time,
            "end_time": end_time,
            "execution_result": execution_result.value if execution_result else None,
        }
        row = (await self._session.execute(_INSERT_SQL, params)).one()
        return row[0]  # type: ignore[no-any-return]
