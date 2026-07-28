"""``AddLibraryAsset`` use case (Slice α9.2).

Adds a registered media asset to the caller's library (explicit opt-in — α9.2 §7.1).
The media asset must be the caller's own live asset (else ``404``); an optional target
folder must likewise be the caller's own (else ``404``). A media asset already in the
library is a ``409`` (``uq_library_assets_media_asset_id``). ``name`` defaults to a
deterministic media-derived label when omitted.
"""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.library._tags import normalize_tags
from app.core.errors import NotFoundError
from app.domain.library.library_asset import LibraryAsset
from app.domain.media.media_asset import MediaAsset


class AddLibraryAsset:
    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        tenant_id: UUID,
        owner_user_id: UUID,
        media_asset_id: UUID,
        library_folder_id: UUID | None = None,
        name: str | None = None,
        description: str | None = None,
        tags: Iterable[str] = (),
    ) -> LibraryAsset:
        async with self._uow:
            media = await self._uow.media.get_owned(media_asset_id, tenant_id, owner_user_id)
            if media is None:
                raise NotFoundError(
                    "media asset not found", details={"media_asset_id": str(media_asset_id)}
                )
            if library_folder_id is not None:
                folder = await self._uow.library.get_folder(
                    library_folder_id, tenant_id, owner_user_id
                )
                if folder is None:
                    raise NotFoundError(
                        "library folder not found",
                        details={"folder_id": str(library_folder_id)},
                    )
            resolved_name = name.strip() if name and name.strip() else _default_name(media)
            asset = await self._uow.library.add_asset(
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                media_asset_id=media_asset_id,
                library_folder_id=library_folder_id,
                name=resolved_name,
                description=description,
                tags=normalize_tags(tags),
            )
            await self._uow.commit()
        return asset


def _default_name(media: MediaAsset) -> str:
    """Deterministic fallback label from the media asset when the client omits ``name``."""
    meta = media.source_metadata or {}
    for key in ("original_filename", "filename", "title"):
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
    return f"{media.kind}-{media.id.hex[:8]}"
