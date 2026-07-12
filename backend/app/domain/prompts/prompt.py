"""``Prompt`` domain entity — the Prompts bounded-context aggregate root.

Mirrors a **slim projection** of the ``prompts`` table (``schema.md`` §11 /
``models/scenes.py``): the physical row also carries ``generated_by_agent``
(server-owned provenance, α8) which the α6.1 domain deliberately omits. Carries
**no** ORM inheritance and no SQLAlchemy awareness. Frozen for value-semantics:
mutations return new instances via ``dataclasses.replace`` at the
repository/use-case layer, keeping the entity immutable for safe sharing across
concurrent tasks — the same discipline as
:class:`app.domain.scenes.scene.Scene`.

Key modelling decisions (PROMPT_AGGREGATE.md / α6.1 pre-flight):

* Prompts are **generation inputs, not editorial content** (ADR-0036): the
  entity has **no ``version`` field** — the ``prompts`` table carries no
  ``VersionMixin`` and does not participate in aggregate OCC, snapshots,
  restore, or diff (Q1/Q8 = Option A). A prompt edit is last-writer-wins.
* ``project_id`` — the owning project (ownership is derived through it; the
  table has no ``tenant_id`` / ``owner_user_id``). Internal; not surfaced as a
  mutation target.
* ``scene_id`` — optional scene link (nullable). ``None`` = a project-level
  prompt; set = scene-scoped. Immutable after create (no re-parenting in α6.1).
* ``kind`` — one of the ``prompt_kind`` enum values (image / video / animation
  / negative / camera / motion / lighting / style).
* ``model_id`` — optional link to the ``ai_models`` registry ("which model is
  this prompt written for"); may be ``None``.
* ``id`` — a durable identity, minted once (``gen_random_uuid()``) and never
  re-minted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Prompt:
    """Prompts aggregate root — one row of the ``prompts`` table (slim view)."""

    id: UUID
    project_id: UUID
    scene_id: UUID | None
    kind: str
    text_content: str
    model_id: UUID | None
    extra: dict[str, Any]
    created_at: datetime
    updated_at: datetime
