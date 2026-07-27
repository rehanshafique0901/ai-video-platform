"""``PromoteGenerationAssets`` — the Asset Promotion Bridge (α8.8).

Implements the ADR-0046 **X8** / W8.6.8 ``PublishGenerationAssets`` seam: it promotes a
completed AI-generation's **final rendered video** (an execution-owned
``generation_assets`` artefact, Path B) into the platform **media library**
(``media_assets(source='generated')``, Path A). Named ``PromoteGenerationAssets`` — not
``Publish…`` — to stay clear of the publishing/destination vocabulary (α8.6).

Boundaries (α8.8 pre-flight):

* **X8 — explicit promotion only.** The Execution Runtime never writes ``media_assets``;
  this use case is the only bridge. It *reads* the generation via the read-only
  :class:`IGenerationReader` port and *writes* media via the ordinary UoW.
* **AP2 — request-time ownership.** Ownership (``tenant_id`` / ``owner_user_id``) is the
  authenticated caller's; ``project_id`` is a required, ownership-validated link (the
  user-initiated adaptation of ``IngestGeneratedMedia``'s project-scoped model). No
  ownership is read from — or written to — the execution plane.
* **AP4 — copy, never reference.** The finished bytes are *copied* into the active media
  store under a deterministic key; the media object's lifecycle is fully independent of
  the execution artefact (which is ``ON DELETE CASCADE`` from ``generations``).
* **AP5 — idempotent.** The deterministic key makes re-promotion collide on the
  ``media_assets`` storage-coordinate uniqueness; the conflict is caught and the existing
  asset returned (``status='noop'``). No new constraint, no migration.
* **AP9 — project-asserted, generation-unowned.** ``generations`` carry no ownership
  (ADR-0046 Q1), so promotion authorizes the *project* (owned by the caller) but does not
  bind the *generation* to an owner; that is deferred to the future generation-trigger
  slice.

Storage note: promotion reads the source bytes through
``storage.resolve(<persisted backend>)``. Today both planes share the one active object
store (``get_generate_video_use_case`` and the media resolver both use
``_get_object_storage()``), so the artefact is reachable; the copy then writes a fresh
key into ``storage.active()``. All storage I/O runs **outside** any DB transaction.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from uuid import UUID

import structlog

from app.application.interfaces.generation_reader import IGenerationReader
from app.application.interfaces.storage_resolver import IStorageResolver
from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.media._links import validate_media_links
from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.domain.generation.execution_state import ExecutionStatus
from app.domain.media.media_asset import MediaAsset

_LOGGER = structlog.get_logger(__name__)

_KIND_VIDEO = "video"
_SOURCE_GENERATED = "generated"


@dataclass(frozen=True, slots=True)
class PromoteGenerationAssetsResult:
    """Outcome of one promotion request.

    ``status`` is ``"promoted"`` when this call registered the media asset, or
    ``"noop"`` when the artefact was already promoted (idempotent replay). ``media`` is
    the resulting library asset either way.
    """

    status: str  # "promoted" | "noop"
    media: MediaAsset
    generation_id: UUID
    generation_asset_id: UUID


def _ext_from_key(key: str) -> str:
    """Return the (cosmetic) ``.ext`` suffix of a storage key, or ``""``."""
    tail = key.rsplit("/", 1)[-1]
    if "." in tail:
        suffix = "." + tail.rsplit(".", 1)[-1]
        if 2 <= len(suffix) <= 6 and suffix[1:].isalnum():
            return suffix.lower()
    return ""


class PromoteGenerationAssets:
    """Promote a completed generation's final video into the media library."""

    def __init__(
        self,
        uow: IUnitOfWork,
        storage: IStorageResolver,
        reader: IGenerationReader,
    ) -> None:
        self._uow = uow
        self._storage = storage
        self._reader = reader

    async def execute(
        self,
        *,
        generation_id: UUID,
        project_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
    ) -> PromoteGenerationAssetsResult:
        # Phase 1 — authorize the project link for the caller (owner-scoped; 422 if foreign).
        async with self._uow:
            await validate_media_links(
                self._uow,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                project_id=project_id,
                scene_id=None,
                prompt_id=None,
                model_id=None,
                validate_model=False,
            )

        # Phase 2 — load the promotable final video (read-only, execution plane).
        video = await self._reader.load_final_video(generation_id)
        if video is None:
            raise NotFoundError(
                "generation not found",
                details={"generation_id": str(generation_id)},
            )
        if video.status != ExecutionStatus.COMPLETED.value or not video.has_final_video:
            raise ValidationFailedError(
                "generation has no promotable final video",
                details={"generation_id": str(generation_id), "status": video.status},
            )

        # Narrow the Optionals for the type-checker (has_final_video guarantees these).
        assert video.final_video_asset_id is not None
        assert video.storage_backend is not None
        assert video.storage_key is not None
        assert video.mime_type is not None
        generation_asset_id = video.final_video_asset_id

        # Phase 3 — copy the bytes into the active media store (OUTSIDE any transaction).
        source_bytes = await self._storage.resolve(video.storage_backend).get(key=video.storage_key)
        media_key = (
            f"{tenant_id}/{project_id}/generation/{generation_id}/"
            f"{generation_asset_id}{_ext_from_key(video.storage_key)}"
        )
        stored = await self._storage.active().put(
            key=media_key, data=source_bytes, content_type=video.mime_type
        )
        checksum = hashlib.sha256(source_bytes).digest()
        size_bytes = len(source_bytes)
        duration_seconds = video.duration_ms / 1000 if video.duration_ms is not None else None
        source_metadata = {
            "origin": "generation_promotion",
            "generation_id": str(generation_id),
            "generation_asset_id": str(generation_asset_id),
            "chosen_adapter": video.chosen_adapter,
            "chosen_provider": video.chosen_provider,
            "seed": video.seed,
        }

        # Phase 4 — register the owned media asset; a duplicate key = idempotent replay.
        media: MediaAsset | None = None
        async with self._uow:
            try:
                media = await self._uow.media.add(
                    tenant_id=tenant_id,
                    owner_user_id=owner_user_id,
                    kind=_KIND_VIDEO,
                    source=_SOURCE_GENERATED,
                    storage_backend=stored.backend,
                    storage_bucket=stored.bucket,
                    storage_key=stored.key,
                    mime_type=video.mime_type,
                    size_bytes=size_bytes,
                    checksum_sha256=checksum,
                    project_id=project_id,
                    scene_id=None,
                    prompt_id=None,
                    model_id=None,
                    provider=video.chosen_provider,
                    width=video.width,
                    height=video.height,
                    duration_seconds=duration_seconds,
                    source_metadata=source_metadata,
                )
                await self._uow.commit()
                status = "promoted"
            except ConflictError:
                status = "noop"

        if status == "noop":
            async with self._uow:
                media = await self._uow.media.get_by_storage_coords(
                    storage_backend=stored.backend,
                    storage_bucket=stored.bucket,
                    storage_key=stored.key,
                )
            if media is None:  # pragma: no cover — the conflict guarantees it exists
                raise ConflictError(
                    "promotion conflicted but the existing asset could not be resolved",
                    details={"generation_id": str(generation_id)},
                )

        assert media is not None
        _LOGGER.info(
            "media.generation_promoted",
            status=status,
            media_id=str(media.id),
            generation_id=str(generation_id),
            generation_asset_id=str(generation_asset_id),
            project_id=str(project_id),
            owner_user_id=str(owner_user_id),
            tenant_id=str(tenant_id),
        )
        return PromoteGenerationAssetsResult(
            status=status,
            media=media,
            generation_id=generation_id,
            generation_asset_id=generation_asset_id,
        )


__all__ = ["PromoteGenerationAssets", "PromoteGenerationAssetsResult"]
