"""Analytics read value objects (Slice α9.0).

The ``analytics_events`` table is append-only and immutable (baseline; reject-mutation
trigger), so the domain models only the *read* projection the creator analytics summary
needs: a per-``event_name`` occurrence count over a time window. There is no write-side
aggregate — the write path is a downstream outbox projection that persists a raw row.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AnalyticsEventCount:
    """One ``(event_name, count)`` row from the owner-scoped analytics aggregate."""

    event_name: str
    count: int
