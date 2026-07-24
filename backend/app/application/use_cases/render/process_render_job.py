"""``ProcessRenderJob`` — render one queued job's Timeline into an output video (α8.4b).

The worker body behind :class:`RenderWorker`. For a single ``queued`` render job it:

1. acquires the ``render_job:<id>`` lease (exactly-once, mirrors the CompletionEngine);
2. claims the job with a ``queued`` → ``running`` CAS;
3. resolves the job's Timeline into an ordered list of video ``MediaAsset``s;
4. **materializes** those source bytes from ``IObjectStorage`` to a temp workspace;
5. composes them via the neutral ``IRenderer`` (FFmpeg in prod, a fake in tests);
6. stores the output under a **deterministic key** and registers a
   ``MediaAsset(kind='video', source='generated')``;
7. settles the job ``succeeded`` (with ``output_media_asset_id``) — or ``failed``.

Invariants:
* **W8.4b.1** — a pure Timeline → Media transform. It neither reads nor mutates
  orchestration state, checkpoints, provider state, workflow status, or the
  completion lifecycle. It touches only ``render_jobs`` lifecycle fields, the
  Timeline (read), ``MediaAsset``s (read sources / create output), storage, and
  render events.
* **W8.4b.2** — consumes only ``MediaAsset`` identifiers + Timeline data. Never
  provider outputs, URLs, checkpoints, request IDs, provider job IDs, or webhooks.

Idempotency: the output storage key is deterministic in the render job, so a
re-render writes identical coordinates and the ``media_assets`` uniqueness raises
``ConflictError`` — caught and resolved to the existing asset. Rendering + all
file I/O happen **outside** any DB transaction (no lock held across CPU work).
"""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import structlog

from app.application.interfaces.object_storage import ObjectStorageError
from app.application.interfaces.renderer import (
    AudioInput,
    IRenderer,
    RenderError,
    RenderInput,
    RenderResult,
    RenderSpec,
)
from app.application.interfaces.storage_resolver import IStorageResolver
from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.render._events import (
    emit_render_job_failed,
    emit_render_job_succeeded,
)
from app.core.errors import ConflictError
from app.domain.render.render_status import RenderStatus

_LOGGER = structlog.get_logger(__name__)

_OUTPUT_MIME = "video/mp4"
_OUTPUT_CONTAINER = "mp4"
_DEFAULT_LEASE = timedelta(seconds=900)


@dataclass(frozen=True, slots=True)
class ProcessRenderJobResult:
    """Outcome of one render pass over a single job."""

    render_job_id: UUID
    status: str  # "rendered" | "failed" | "skipped" | "noop"
    output_media_asset_id: UUID | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class _ResolvedClip:
    """A resolved video-track clip (contributes video + its own synced audio)."""

    media_asset_id: UUID
    storage_backend: str
    storage_bucket: str
    storage_key: str
    source_start_seconds: float
    source_end_seconds: float
    start_seconds: float
    volume: float  # α8.4e: clip audio gain
    muted: bool  # α8.4e: owning track's mute flag


@dataclass(frozen=True, slots=True)
class _ResolvedAudio:
    """A resolved audio-track clip (music / voiceover) overlaid on the composition (α8.4e)."""

    media_asset_id: UUID
    storage_backend: str
    storage_bucket: str
    storage_key: str
    source_start_seconds: float
    source_end_seconds: float
    start_seconds: float
    volume: float


class ProcessRenderJob:
    """Render a single ``queued`` job under its own lease (Timeline → output MediaAsset)."""

    def __init__(
        self,
        uow: IUnitOfWork,
        storage: IStorageResolver,
        renderer: IRenderer,
        *,
        workspace_dir: str | None = None,
        owner: str | None = None,
        lease: timedelta = _DEFAULT_LEASE,
    ) -> None:
        self._uow = uow
        self._storage = storage
        self._renderer = renderer
        self._workspace_dir = workspace_dir
        self._owner = owner or f"render-worker:{uuid4()}"
        self._lease = lease

    async def process(self, *, project_id: UUID, render_job_id: UUID) -> ProcessRenderJobResult:
        lock_key = f"render_job:{render_job_id}"
        async with self._uow:
            lease = await self._uow.locks.acquire(
                key=lock_key, owner=self._owner, lease=self._lease
            )
            await self._uow.commit()
        if lease is None:
            _LOGGER.info("render.locked", render_job_id=str(render_job_id))
            return ProcessRenderJobResult(
                render_job_id=render_job_id, status="skipped", reason="locked"
            )

        try:
            # Phase 1 — claim (queued → running CAS) + resolve ownership/timeline.
            async with self._uow:
                job = await self._uow.render_jobs.get_owned(project_id, render_job_id)
                if job is None or job.status != RenderStatus.QUEUED.value:
                    return ProcessRenderJobResult(
                        render_job_id=render_job_id, status="noop", reason="not_queued"
                    )
                ownership = await self._uow.projects.get_ownership(project_id)
                if ownership is None:
                    return ProcessRenderJobResult(
                        render_job_id=render_job_id, status="noop", reason="no_ownership"
                    )
                claimed = await self._uow.render_jobs.mark_running(render_job_id)
                if claimed is None:
                    await self._uow.rollback()
                    return ProcessRenderJobResult(
                        render_job_id=render_job_id, status="skipped", reason="claim_lost"
                    )
                await self._uow.commit()

            tenant_id, owner_user_id = ownership
            timeline_id = job.timeline_id

            try:
                return await self._render_and_settle(
                    project_id=project_id,
                    render_job_id=render_job_id,
                    timeline_id=timeline_id,
                    tenant_id=tenant_id,
                    owner_user_id=owner_user_id,
                )
            except (RenderError, ObjectStorageError) as exc:
                return await self._settle_failed(render_job_id, exc)
        finally:
            async with self._uow:
                await self._uow.locks.release(lease)
                await self._uow.commit()

    async def _render_and_settle(
        self,
        *,
        project_id: UUID,
        render_job_id: UUID,
        timeline_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
    ) -> ProcessRenderJobResult:
        # Phase 2a — resolve the Timeline into ordered video clips + audio-track clips.
        resolved, audio_resolved = await self._resolve_composition(
            timeline_id=timeline_id, tenant_id=tenant_id, owner_user_id=owner_user_id
        )
        if not resolved:
            raise RenderError("timeline has no renderable video clips")

        # Phase 2b — materialize + compose + store, ALL outside any DB transaction.
        with tempfile.TemporaryDirectory(dir=self._workspace_dir) as tmp:
            tmp_path = Path(tmp)
            inputs: list[RenderInput] = []
            for i, clip in enumerate(resolved):
                src = await self._materialize(clip, tmp_path / f"in_{i:04d}")
                inputs.append(
                    RenderInput(
                        path=src,
                        source_start_seconds=clip.source_start_seconds,
                        source_end_seconds=clip.source_end_seconds,
                        volume=clip.volume,
                        muted=clip.muted,
                    )
                )

            audio_inputs: list[AudioInput] = []
            for k, aclip in enumerate(audio_resolved):
                src = await self._materialize(aclip, tmp_path / f"aud_{k:04d}")
                audio_inputs.append(
                    AudioInput(
                        path=src,
                        source_start_seconds=aclip.source_start_seconds,
                        source_end_seconds=aclip.source_end_seconds,
                        start_seconds=aclip.start_seconds,
                        volume=aclip.volume,
                    )
                )

            out_path = tmp_path / f"out.{_OUTPUT_CONTAINER}"
            result: RenderResult = await self._renderer.render(
                RenderSpec(
                    inputs=tuple(inputs),
                    output_path=str(out_path),
                    container=_OUTPUT_CONTAINER,
                    audio_inputs=tuple(audio_inputs),
                )
            )
            output_bytes = await asyncio.to_thread(Path(result.output_path).read_bytes)

        checksum = hashlib.sha256(output_bytes).digest()
        output_key = f"renders/{tenant_id}/{project_id}/{render_job_id}.{_OUTPUT_CONTAINER}"
        stored = await self._storage.active().put(
            key=output_key, data=output_bytes, content_type=_OUTPUT_MIME
        )

        # Phase 3 — register the output MediaAsset + settle the job succeeded.
        async with self._uow:
            output_media_asset_id = await self._register_output(
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                project_id=project_id,
                render_job_id=render_job_id,
                timeline_id=timeline_id,
                stored_backend=stored.backend,
                stored_bucket=stored.bucket,
                stored_key=stored.key,
                size_bytes=len(output_bytes),
                checksum=checksum,
                result=result,
            )
            settled = await self._uow.render_jobs.mark_succeeded(
                render_job_id, output_media_asset_id=output_media_asset_id
            )
            if settled is None:
                # Canceled mid-render: the output asset is registered but the job is
                # no longer running. Do not resurrect it — commit the asset only.
                await self._uow.commit()
                _LOGGER.info(
                    "render.not_running_at_settle",
                    render_job_id=str(render_job_id),
                    output_media_asset_id=str(output_media_asset_id),
                )
                return ProcessRenderJobResult(
                    render_job_id=render_job_id,
                    status="noop",
                    reason="not_running",
                    output_media_asset_id=output_media_asset_id,
                )
            await emit_render_job_succeeded(self._uow, settled)
            await self._uow.commit()

        _LOGGER.info(
            "render.succeeded",
            render_job_id=str(render_job_id),
            project_id=str(project_id),
            output_media_asset_id=str(output_media_asset_id),
            clips=len(resolved),
        )
        return ProcessRenderJobResult(
            render_job_id=render_job_id,
            status="rendered",
            output_media_asset_id=output_media_asset_id,
        )

    async def _resolve_composition(
        self, *, timeline_id: UUID, tenant_id: UUID, owner_user_id: UUID
    ) -> tuple[list[_ResolvedClip], list[_ResolvedAudio]]:
        """Read the Timeline and resolve its video clips + audio-track clips.

        Video (α8.4b, unchanged): clips resolving to a *video* asset, in chronological
        order ``(start_seconds, media_asset_id)`` — a total order (Fork D1: sequential
        concat). Each carries ``clip.volume`` and its owning track's ``muted`` flag so
        the clip's own audio can travel with its segment (α8.4e).

        Audio (α8.4e): clips on **audio-kind** tracks that are **not muted**, resolving
        to an audio- or video-bearing asset, overlaid at ``clip.start_seconds``. Ordered
        by ``(start_seconds, media_asset_id)`` for a deterministic input order.

        Clips with no ``media_asset_id`` or an unresolved/foreign asset are skipped.
        """
        async with self._uow:
            tracks = await self._uow.timeline.list_tracks(timeline_id)
            track_by_id = {t.id: t for t in tracks}
            clips_by_track = await self._uow.timeline.list_clips_for_timeline(timeline_id)

            resolved: list[_ResolvedClip] = []
            audio_resolved: list[_ResolvedAudio] = []
            for track_id, clips in clips_by_track.items():
                track = track_by_id.get(track_id)
                is_audio_track = track is not None and track.kind == "audio"
                track_muted = track.muted if track is not None else False
                for clip in clips:
                    if clip.media_asset_id is None:
                        continue
                    asset = await self._uow.media.get_owned(
                        clip.media_asset_id, tenant_id, owner_user_id
                    )
                    if asset is None:
                        continue
                    if is_audio_track:
                        # Muted audio track contributes nothing (neither video nor audio).
                        if track_muted or asset.kind not in ("audio", "video"):
                            continue
                        audio_resolved.append(
                            _ResolvedAudio(
                                media_asset_id=asset.id,
                                storage_backend=asset.storage_backend,
                                storage_bucket=asset.storage_bucket,
                                storage_key=asset.storage_key,
                                source_start_seconds=clip.source_start_seconds,
                                source_end_seconds=clip.source_end_seconds,
                                start_seconds=clip.start_seconds,
                                volume=clip.volume,
                            )
                        )
                        continue
                    if asset.kind != "video":
                        continue
                    resolved.append(
                        _ResolvedClip(
                            media_asset_id=asset.id,
                            storage_backend=asset.storage_backend,
                            storage_bucket=asset.storage_bucket,
                            storage_key=asset.storage_key,
                            source_start_seconds=clip.source_start_seconds,
                            source_end_seconds=clip.source_end_seconds,
                            start_seconds=clip.start_seconds,
                            volume=clip.volume,
                            muted=track_muted,
                        )
                    )
        resolved.sort(key=lambda c: (c.start_seconds, str(c.media_asset_id)))
        audio_resolved.sort(key=lambda a: (a.start_seconds, str(a.media_asset_id)))
        return resolved, audio_resolved

    async def _materialize(self, clip: _ResolvedClip | _ResolvedAudio, dest: Path) -> str:
        """Fetch a resolved clip's bytes from storage into ``dest``; return its path.

        Reads always resolve by the clip's *persisted* backend (W8.5b.4 / W8.5b.5), never the
        active write backend — existing source media stays readable wherever it actually lives.
        Enforces that the source lives in the resolved storage location (W8.4b.2: only
        ``MediaAsset`` coordinates are consumed, never provider URLs).
        """
        source = self._storage.resolve(clip.storage_backend)
        if clip.storage_bucket != source.bucket:
            raise RenderError(
                "source media is not in the render storage location "
                f"({clip.storage_backend}/{clip.storage_bucket})"
            )
        data = await source.get(key=clip.storage_key)
        await asyncio.to_thread(dest.write_bytes, data)
        return str(dest)

    async def _register_output(
        self,
        *,
        tenant_id: UUID,
        owner_user_id: UUID,
        project_id: UUID,
        render_job_id: UUID,
        timeline_id: UUID,
        stored_backend: str,
        stored_bucket: str,
        stored_key: str,
        size_bytes: int,
        checksum: bytes,
        result: RenderResult,
    ) -> UUID:
        """Register the render-output ``MediaAsset``; recover the existing one on conflict."""
        try:
            asset = await self._uow.media.add(
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                kind="video",
                source="generated",
                storage_backend=stored_backend,
                storage_bucket=stored_bucket,
                storage_key=stored_key,
                mime_type=_OUTPUT_MIME,
                size_bytes=size_bytes,
                checksum_sha256=checksum,
                project_id=project_id,
                scene_id=None,
                prompt_id=None,
                model_id=None,
                provider=None,
                width=result.width,
                height=result.height,
                duration_seconds=result.duration_seconds,
                source_metadata={
                    "origin": "render",
                    "render_job_id": str(render_job_id),
                    "timeline_id": str(timeline_id),
                },
            )
            return asset.id
        except ConflictError:
            existing = await self._uow.media.get_by_storage_coords(
                storage_backend=stored_backend,
                storage_bucket=stored_bucket,
                storage_key=stored_key,
            )
            if existing is None:  # pragma: no cover - constraint says it must exist
                raise
            return existing.id

    async def _settle_failed(self, render_job_id: UUID, exc: Exception) -> ProcessRenderJobResult:
        message = str(exc)[:500]
        error: dict[str, object] = {"code": "render_failed", "message": message}
        async with self._uow:
            failed = await self._uow.render_jobs.mark_failed(render_job_id, error=error)
            if failed is not None:
                await emit_render_job_failed(self._uow, failed, error=error)
                await self._uow.commit()
            else:
                await self._uow.rollback()
        _LOGGER.warning("render.failed", render_job_id=str(render_job_id), error=message)
        return ProcessRenderJobResult(render_job_id=render_job_id, status="failed", reason=message)
