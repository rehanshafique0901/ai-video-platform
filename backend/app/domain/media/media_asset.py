"""``MediaAsset`` domain entity — the Media bounded-context aggregate root.

Mirrors a **slim projection** of the ``media_assets`` table (``schema.md`` §12 /
``models/media.py``). Carries **no** ORM inheritance and no SQLAlchemy
awareness. Frozen for value-semantics: mutations return new instances via
``dataclasses.replace`` at the repository/use-case layer, keeping the entity
immutable for safe sharing across concurrent tasks — the same discipline as
:class:`app.domain.prompts.prompt.Prompt` and
:class:`app.domain.scenes.scene.Scene`.

Key modelling decisions (MEDIA_AGGREGATE.md / α6.2 pre-flight):

* Media assets are **generation outputs, not editorial content** (ADR-0037,
  which adopts ADR-0036): the entity has **no ``version`` field** — the
  ``media_assets`` table carries no ``VersionMixin`` and does not participate in
  aggregate OCC, snapshots, restore, or diff (Q3). A media edit is
  last-writer-wins.
* **Direct ownership** — ``tenant_id`` + ``owner_user_id`` are on the row and
  NOT NULL (contrast prompts/scenes, whose ownership is derived through a
  project). They are internal identity/authorization fields, not surfaced as
  mutation targets and omitted from the public DTO (caller-implicit).
* ``project_id`` / ``scene_id`` / ``prompt_id`` — optional links (all nullable).
  ``None`` = an owner-level asset not tied to that entity. Mutable (re-link) via
  a narrow PATCH (Q8).
* ``model_id`` — optional link to the ``ai_models`` registry (which model
  produced/should produce this asset); may be ``None``. Mutable (Q8).
* ``kind`` — one of the ``media_kind`` enum values (image / video / narration /
  subtitle / music / sound_effect / thumbnail). **Immutable** after register.
* ``source`` — one of ``media_source`` (uploaded / stock / generated); α6.2
  only registers uploaded / stock. **Immutable** after register.
* Physical-object fields — ``storage_backend`` / ``storage_bucket`` /
  ``storage_key`` (unique together), ``mime_type``, ``size_bytes``,
  ``checksum_sha256`` (raw 32 bytes), ``width`` / ``height`` /
  ``duration_seconds`` — describe the concrete stored object and are
  **immutable forever** (changing them means it is a *different* asset, Q8).
* ``id`` — a durable identity, minted once (``gen_random_uuid()``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(frozen=True, slots=True)
class MediaAsset:
    """Media aggregate root — one row of the ``media_assets`` table (slim view)."""

    id: UUID
    tenant_id: UUID
    owner_user_id: UUID
    kind: str
    project_id: UUID | None
    scene_id: UUID | None
    prompt_id: UUID | None
    model_id: UUID | None
    provider: str | None
    storage_backend: str
    storage_bucket: str
    storage_key: str
    mime_type: str
    size_bytes: int
    width: int | None
    height: int | None
    duration_seconds: float | None
    checksum_sha256: bytes
    source: str
    source_metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime
