"""``LibraryFolder`` domain entity — a node in the owner's library folder tree.

Mirrors a slim projection of the ``library_folders`` table (``models/media.py`` /
``0001_baseline.py``). Frozen for value-semantics (like
:class:`app.domain.media.media_asset.MediaAsset`): mutations return new instances
at the repository/use-case layer.

Modelling decisions (α9.2 pre-flight §2):

* **Direct ownership** — ``tenant_id`` + ``owner_user_id`` are on the row and NOT
  NULL. Internal identity/authorization fields, omitted from the public DTO.
* ``parent_folder_id`` — optional self-reference (``None`` = a root folder). A
  folder cannot be its own parent (DB ``CheckConstraint("id <> parent_folder_id")``);
  deeper cycles are prevented at the use-case layer on move.
* **No ``version``** — unlike ``library_assets``, ``library_folders`` carries no
  ``VersionMixin``; folder edits are last-writer-wins (name/parent), scoped by the
  owner + the ``(parent_folder_id, name)`` live-row partial-unique index.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class LibraryFolder:
    """One row of the ``library_folders`` table (slim view)."""

    id: UUID
    tenant_id: UUID
    owner_user_id: UUID
    parent_folder_id: UUID | None
    name: str
    created_at: datetime
    updated_at: datetime
