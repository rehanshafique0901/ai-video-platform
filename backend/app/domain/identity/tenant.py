"""``Tenant`` domain entity — the workspace / billing container.

Mirrors the ``tenants`` table (``schema.md`` §1). One tenant per
self-service signup in α2a per the approved plan; future invitation
flows add more users to the same tenant. ``plan_tier`` is informational
here; the source of truth for the billing tier is the ``subscriptions``
table (schema §22), which is queried separately by billing use cases.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Tenant:
    """Workspace aggregate root — one row of the ``tenants`` table."""

    id: UUID
    name: str
    slug: str
    plan_tier: str
    created_at: datetime
    updated_at: datetime
