"""``Session`` domain entity — one refresh-token rotation slot.

Mirrors the ``sessions`` table (``schema.md`` §4). Each row represents
one *issued* refresh token for one *family*. Rotation on refresh (α2b)
creates a new row with the **same** ``family_id`` and a new ``id`` +
``token_hash``, marking the old row as revoked. Replay of a revoked
token in α2b revokes the entire family — that logic belongs to the
``RefreshSession`` use case, not to the domain entity.

``token_hash`` stores ``sha256(refresh_jwt)`` in lowercase hex so the
raw refresh JWT is never persisted (defence in depth for a database
compromise).

α2a creates one ``Session`` per successful register + one per
successful login. α2b handles refresh + logout.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Session:
    """Refresh-token session — one row of the ``sessions`` table."""

    id: UUID
    user_id: UUID
    family_id: UUID
    token_hash: str
    ip: str | None
    user_agent: str | None
    issued_at: datetime
    last_used_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
