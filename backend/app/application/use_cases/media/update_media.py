"""``UpdateMedia`` use case (Slice α6.2, narrow PATCH).

Contract (API_CONTRACT §3.2.3):

    PATCH /api/v1/media/{media_id}
      body:  { project_id?, scene_id?, prompt_id?, model_id?, provider?,
               source_metadata? }   (tri-state; storage/checksum/size/etc. immutable)
      → 200  { data: MediaPublic (updated_at advanced on real change), meta }
      → 404  { error: { code: NOT_FOUND, ... } }         (missing / not yours / deleted)
      → 422  { error: { code: VALIDATION_FAILED, ... } } (bad body OR bad link)
      → 401  { error: { code: UNAUTHENTICATED, ... } }   (via CurrentUserDep)

**No version fence / no 412** (ADR-0037): media has no OCC column, so a PATCH is
last-writer-wins. Only the **mutable** subset reaches ``changes`` (the four links
+ ``provider`` + ``source_metadata`` — Q8); the physical-object columns
(``storage_*`` / ``checksum`` / ``mime`` / ``size`` / dimensions / ``kind`` /
``source``) are immutable and rejected by the DTO (``extra="forbid"``) with
``422`` before this use case runs. Link re-validation (Q5) fires only when a link
actually changes; a same-value patch is a no-op (no write, ``updated_at``
unchanged); the empty patch is rejected upstream (``422``).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

import structlog

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.media._links import validate_media_links
from app.core.errors import NotFoundError, ValidationFailedError
from app.domain.media.media_asset import MediaAsset

_LOGGER = structlog.get_logger(__name__)

_PROJECT_LINK_FIELDS = frozenset({"project_id", "scene_id", "prompt_id"})


class UpdateMedia:
    """Narrow partial update of a media asset owned by the caller (no OCC)."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        media_id: UUID,
        owner_user_id: UUID,
        tenant_id: UUID,
        changes: Mapping[str, Any],
        ip: str | None = None,
    ) -> MediaAsset:
        async with self._uow:
            media = await self._uow.media.get_owned(media_id, tenant_id, owner_user_id)
            if media is None:
                _LOGGER.warning(
                    "media.update_rejected",
                    reason="not_visible",
                    media_id=str(media_id),
                    owner_user_id=str(owner_user_id),
                    ip=ip,
                )
                raise NotFoundError(
                    "media asset not found",
                    details={"media_id": str(media_id)},
                )

            # Re-validate the *effective* project/scene/prompt combination iff a
            # project-link field changed — moving to a new project must keep any
            # retained scene/prompt consistent (Q5). ``changes.get(k, current)``
            # yields the post-patch value (explicit ``null`` clears).
            if _PROJECT_LINK_FIELDS & changes.keys():
                await validate_media_links(
                    self._uow,
                    tenant_id=tenant_id,
                    owner_user_id=owner_user_id,
                    project_id=changes.get("project_id", media.project_id),
                    scene_id=changes.get("scene_id", media.scene_id),
                    prompt_id=changes.get("prompt_id", media.prompt_id),
                    model_id=None,
                    validate_model=False,
                )

            # Validate a (re)linked model independently (skip on clear/untouched).
            new_model_id = changes.get("model_id")
            if (
                "model_id" in changes
                and new_model_id is not None
                and not await self._uow.media.model_is_linkable(new_model_id)
            ):
                raise ValidationFailedError(
                    "model_id does not reference a linkable model",
                    details={"field": "model_id", "model_id": str(new_model_id)},
                )

            effective = {k: v for k, v in changes.items() if getattr(media, k) != v}
            if not effective:
                _LOGGER.info(
                    "media.update_rejected",
                    reason="same_value_noop",
                    media_id=str(media.id),
                    owner_user_id=str(owner_user_id),
                    ip=ip,
                )
                return media

            updated = await self._uow.media.update_owned(
                media_id, tenant_id, owner_user_id, effective
            )
            if updated is None:
                # No OCC fence: a None here means the row was soft-deleted
                # between the visibility gate and the write → uniform 404.
                _LOGGER.warning(
                    "media.update_rejected",
                    reason="not_visible",
                    media_id=str(media_id),
                    owner_user_id=str(owner_user_id),
                    ip=ip,
                )
                raise NotFoundError(
                    "media asset not found",
                    details={"media_id": str(media_id)},
                )
            # No aggregate OCC bump (ADR-0037).
            await self._uow.commit()

        _LOGGER.info(
            "media.updated",
            media_id=str(updated.id),
            changed_fields=sorted(effective.keys()),
            owner_user_id=str(owner_user_id),
            ip=ip,
        )
        return updated
