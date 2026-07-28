"""``LibraryAsset`` domain entity — a curated library entry over a registered media asset.

Mirrors a slim projection of the ``library_assets`` table (``models/media.py`` /
``0001_baseline.py``). Frozen for value-semantics.

Modelling decisions (α9.2 pre-flight §2; ADR-0037 CR-8):

* A library entry **wraps one registered ``media_asset``** (``media_asset_id`` is
  NOT NULL and UNIQUE — ``uq_library_assets_media_asset_id`` → one entry per asset).
  The library never mutates ``media_assets``; it only references it.
* **Direct ownership** — ``tenant_id`` + ``owner_user_id`` NOT NULL, omitted from the
  public DTO.
* ``library_folder_id`` — optional placement (``None`` = unfiled). Cleared to
  ``NULL`` when its folder is soft-deleted (SET NULL FK + the α9.2 rule).
* ``tags`` — a ``text[]`` (empty by default); browsed via the existing GIN index.
  Modelled as an immutable ``tuple[str, ...]`` in the domain.
* ``usage_count`` / ``last_used_at`` — reuse counters advanced by ``record_use``.
* ``version`` — the ``VersionMixin`` OCC handle (ADR-0037 CR-8); library-asset edits
  are version-fenced (404-before-412), unlike ``media_assets`` (last-writer-wins).
* The dormant ``embedding vector(1536)`` column is **not** modelled — semantic/vector
  search is deliberately out of scope for α9.2 (its own future increment + ADR).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class LibraryAsset:
    """One row of the ``library_assets`` table (slim view)."""

    id: UUID
    tenant_id: UUID
    owner_user_id: UUID
    media_asset_id: UUID
    library_folder_id: UUID | None
    name: str
    description: str | None
    tags: tuple[str, ...]
    usage_count: int
    last_used_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime
