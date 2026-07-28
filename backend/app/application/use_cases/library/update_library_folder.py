"""``UpdateLibraryFolder`` use case (Slice α9.2).

Last-writer-wins partial update of the caller's own folder (``name`` and/or
``parent_folder_id``). Folders carry no OCC version. Rules:

* **404-before-everything.** Missing / not-yours / soft-deleted → uniform ``404``.
* **Same-value no-op.** A patch that changes nothing returns the current row (``200``),
  no write.
* **Move validation (α9.2 §7.3).** Re-parenting under a folder that is not the caller's
  own live folder → ``404``; moving a folder under itself or one of its descendants
  → ``422`` (cycle).
* **Name collision** under the target parent → ``409``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.domain.library.library_folder import LibraryFolder

# Defensive bound on the ancestor walk (owner folder trees are shallow); guards
# against a pathological/corrupt chain rather than a normal hierarchy.
_MAX_ANCESTOR_WALK = 10_000


class UpdateLibraryFolder:
    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        folder_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
        changes: Mapping[str, Any],
    ) -> LibraryFolder:
        async with self._uow:
            folder = await self._uow.library.get_folder(folder_id, tenant_id, owner_user_id)
            if folder is None:
                raise NotFoundError(
                    "library folder not found", details={"folder_id": str(folder_id)}
                )

            effective = {k: v for k, v in changes.items() if getattr(folder, k) != v}
            if not effective:
                return folder

            if "parent_folder_id" in effective and effective["parent_folder_id"] is not None:
                await self._validate_move(
                    folder_id=folder_id,
                    new_parent_id=effective["parent_folder_id"],
                    tenant_id=tenant_id,
                    owner_user_id=owner_user_id,
                )

            if "name" in effective or "parent_folder_id" in effective:
                target_name = effective.get("name", folder.name)
                target_parent = effective.get("parent_folder_id", folder.parent_folder_id)
                if await self._uow.library.folder_name_conflicts(
                    tenant_id=tenant_id,
                    owner_user_id=owner_user_id,
                    parent_folder_id=target_parent,
                    name=target_name,
                    exclude_folder_id=folder_id,
                ):
                    raise ConflictError("library folder already exists")

            updated = await self._uow.library.update_folder(
                folder_id, tenant_id, owner_user_id, effective
            )
            if updated is None:
                raise NotFoundError(
                    "library folder not found", details={"folder_id": str(folder_id)}
                )
            await self._uow.commit()
        return updated

    async def _validate_move(
        self,
        *,
        folder_id: UUID,
        new_parent_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
    ) -> None:
        current: UUID | None = new_parent_id
        steps = 0
        while current is not None and steps < _MAX_ANCESTOR_WALK:
            if current == folder_id:
                raise ValidationFailedError(
                    "cannot move a folder under itself or one of its descendants",
                    details={"folder_id": str(folder_id)},
                )
            node = await self._uow.library.get_folder(current, tenant_id, owner_user_id)
            if node is None:
                if current == new_parent_id:
                    # The requested target parent is not the caller's live folder.
                    raise NotFoundError(
                        "library folder not found",
                        details={"folder_id": str(new_parent_id)},
                    )
                # An ancestor is no longer visible; the chain cannot reach folder_id.
                return
            current = node.parent_folder_id
            steps += 1
