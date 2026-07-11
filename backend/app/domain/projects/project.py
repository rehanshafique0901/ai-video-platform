"""``Project`` domain entity — the Projects bounded-context aggregate root.

Mirrors the ``projects`` table (``schema.md`` §6) but carries **no** ORM
inheritance and no SQLAlchemy awareness. Frozen for value-semantics:
mutations return new instances via ``dataclasses.replace`` at the
use-case layer (α5b onward), keeping the entity immutable for safe
sharing across concurrent tasks — the same discipline as
:class:`app.domain.identity.user.User`.

See ``docs/domain/PROJECT_AGGREGATE.md`` for the full aggregate model.
α5a (create + read) populates ``name`` / ``aspect_ratio`` /
``description`` / ``language`` / ``style`` / ``settings`` from the
caller; ``folder_id`` / ``current_version_id`` / ``duration_seconds``
are left at their defaults (``None``) until later slices wire folders,
the version ledger, and render output. The entity still mirrors those
columns faithfully so the repository can round-trip a full row.

Two versioning fields deliberately coexist (aggregate doc §6):

* ``version`` — the optimistic-concurrency counter (``VersionMixin``).
  Bumped on every persisted mutation; the α5b ``PATCH`` fence reads it.
* ``current_version_id`` — a pointer into the immutable
  ``project_versions`` content-snapshot ledger, managed by a later
  slice. Unset in α5a.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Project:
    """Projects aggregate root — one row of the ``projects`` table."""

    id: UUID
    tenant_id: UUID
    owner_user_id: UUID
    folder_id: UUID | None
    current_version_id: UUID | None
    name: str
    description: str | None
    aspect_ratio: str
    duration_seconds: float | None
    language: str
    style: str | None
    settings: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    version: int
