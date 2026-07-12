"""``RegisterMedia`` use case (Slice α6.2).

Contract (API_CONTRACT §3.2.3):

    POST /api/v1/media
      body:  { kind, source, storage_backend, storage_bucket, storage_key,
               mime_type, size_bytes, checksum_sha256, project_id?, scene_id?,
               prompt_id?, model_id?, provider?, width?, height?,
               duration_seconds?, source_metadata? }
      → 201  { data: MediaPublic, meta }
      → 409  { error: { code: CONFLICT, ... } }           (duplicate storage coords)
      → 422  { error: { code: VALIDATION_FAILED, ... } }  (bad body OR bad link)
      → 401  { error: { code: UNAUTHENTICATED, ... } }    (via CurrentUserDep)

**Register-by-metadata (α6.2 Q2).** The client supplies storage coordinates for
an object it already holds; this use case makes **no** provider or
object-storage call. ``source`` is restricted to ``uploaded`` / ``stock`` by the
DTO (``generated`` is an α8 concern).

Flow:

1. **Link validation** (Q5, shared ``validate_media_links``) — each present
   ``project_id`` / ``scene_id`` / ``prompt_id`` / ``model_id`` must be
   owned/live/linkable; a foreign/unknown/soft-deleted link, or a scene/prompt
   without a project, → ``422``.
2. **Insert** and commit. Ownership (``owner_user_id`` / ``tenant_id``) comes
   from ``CurrentUserDep``, never the body. A ``(storage_backend,
   storage_bucket, storage_key)`` collision raises ``ConflictError`` → ``409``
   (Q6). Per ADR-0037 this does **NOT** bump ``projects.version`` and is **NOT**
   captured in any snapshot.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.media._links import validate_media_links
from app.domain.media.media_asset import MediaAsset

_LOGGER = structlog.get_logger(__name__)


class RegisterMedia:
    """Register a media asset (metadata only) owned by the caller."""

    def __init__(self, uow: IUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self,
        *,
        owner_user_id: UUID,
        tenant_id: UUID,
        kind: str,
        source: str,
        storage_backend: str,
        storage_bucket: str,
        storage_key: str,
        mime_type: str,
        size_bytes: int,
        checksum_sha256: bytes,
        project_id: UUID | None = None,
        scene_id: UUID | None = None,
        prompt_id: UUID | None = None,
        model_id: UUID | None = None,
        provider: str | None = None,
        width: int | None = None,
        height: int | None = None,
        duration_seconds: float | None = None,
        source_metadata: dict[str, Any] | None = None,
        ip: str | None = None,
    ) -> MediaAsset:
        async with self._uow:
            await validate_media_links(
                self._uow,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                project_id=project_id,
                scene_id=scene_id,
                prompt_id=prompt_id,
                model_id=model_id,
                validate_model=True,
            )

            media = await self._uow.media.add(
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                kind=kind,
                source=source,
                storage_backend=storage_backend,
                storage_bucket=storage_bucket,
                storage_key=storage_key,
                mime_type=mime_type,
                size_bytes=size_bytes,
                checksum_sha256=checksum_sha256,
                project_id=project_id,
                scene_id=scene_id,
                prompt_id=prompt_id,
                model_id=model_id,
                provider=provider,
                width=width,
                height=height,
                duration_seconds=duration_seconds,
                source_metadata=source_metadata or {},
            )
            # No aggregate OCC bump (ADR-0037): media is a generation output,
            # not versioned editorial content.
            await self._uow.commit()

        _LOGGER.info(
            "media.registered",
            media_id=str(media.id),
            kind=kind,
            source=source,
            storage_backend=storage_backend,
            project_id=None if project_id is None else str(project_id),
            scene_id=None if scene_id is None else str(scene_id),
            prompt_id=None if prompt_id is None else str(prompt_id),
            model_id=None if model_id is None else str(model_id),
            size_bytes=size_bytes,
            owner_user_id=str(owner_user_id),
            tenant_id=str(tenant_id),
            ip=ip,
        )
        return media
