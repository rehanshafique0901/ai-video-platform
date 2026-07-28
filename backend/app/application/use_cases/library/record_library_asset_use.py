"""``RecordLibraryAssetUse`` use case (Slice α9.2 §7.6).

Records that the caller reused a library asset in one of their projects: increments
``usage_count``, stamps ``last_used_at``, and upserts the ``library_asset_projects``
junction (idempotent per ``(asset, project)``). Both the asset and the project must be
the caller's own live rows (else ``404``).

Note: because ``library_assets`` carries a version-bump trigger, recording a use advances
the asset's OCC ``version`` by 1 (a reuse is a genuine mutation) — the updated asset is
returned so the client can refresh its optimistic-concurrency handle.
"""

from __future__ import annotations

from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.core.errors import NotFoundError
from app.domain.library.library_asset import LibraryAsset


class RecordLibraryAssetUse:
    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        asset_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
        project_id: UUID,
    ) -> LibraryAsset:
        async with self._uow:
            project = await self._uow.projects.get_owned(
                project_id=project_id, tenant_id=tenant_id, owner_user_id=owner_user_id
            )
            if project is None:
                raise NotFoundError("project not found", details={"project_id": str(project_id)})
            # Establish visibility (also hides an entry with soft-deleted media).
            asset = await self._uow.library.get_asset(asset_id, tenant_id, owner_user_id)
            if asset is None:
                raise NotFoundError(
                    "library asset not found", details={"library_asset_id": str(asset_id)}
                )
            updated = await self._uow.library.record_use(
                asset_id, tenant_id, owner_user_id, project_id=project_id
            )
            if updated is None:
                raise NotFoundError(
                    "library asset not found", details={"library_asset_id": str(asset_id)}
                )
            await self._uow.commit()
        return updated
