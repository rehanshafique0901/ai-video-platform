"""``User`` domain entity — the identity aggregate root.

Mirrors the ``users`` table (``schema.md`` §2) but carries **no** ORM
inheritance and no SQLAlchemy awareness. Frozen for value-semantics:
mutations return new instances via ``dataclasses.replace`` at the use-case
layer, keeping the entity itself immutable for safe sharing across
concurrent tasks.

``password_hash`` is ``None`` for users that only ever authenticate via
OAuth (deferred to α5). α2a always produces users with a non-null
Argon2id ``password_hash`` because the only registration path is
password-based.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class User:
    """Identity aggregate root — one row of the ``users`` table."""

    id: UUID
    tenant_id: UUID
    email: str
    password_hash: str | None
    display_name: str
    email_verified_at: datetime | None
    last_login_at: datetime | None
    created_at: datetime
    updated_at: datetime
    version: int
