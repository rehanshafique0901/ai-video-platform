"""``IngestGeneratedMedia`` — turn a succeeded run's provider output into media (α8.4a).

The platform's first *producing* use case. For a `succeeded` workflow run it reads
the (already-finalized) provider output envelopes from the run's steps, downloads
each ``image_ref`` / ``video_ref`` artifact, stores the bytes via ``IObjectStorage``,
and registers a ``MediaAsset(source='generated')``.

Invariants:
* **W8.4.1** — strictly downstream of the frozen completion pipeline; it never
  touches orchestration internals.
* **W8.4.2** — *observational*: it only **reads** ``WorkflowRun`` / steps and
  **creates** downstream artifacts (storage objects, ``MediaAsset`` rows). It never
  mutates a run, checkpoint, step, or usage record.

Idempotency: the storage key is **deterministic** in ``(run, step, request_id)``,
so a redelivered ``WorkflowRunSucceeded`` re-writes identical bytes and the
``media_assets`` storage-key uniqueness raises ``ConflictError`` on re-registration
— caught and treated as an already-ingested no-op. Downloads happen **outside** any
DB transaction (provider/CDN I/O never holds a lock open).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import structlog

from app.application.interfaces.media_downloader import IMediaDownloader
from app.application.interfaces.object_storage import IObjectStorage
from app.application.interfaces.unit_of_work import IUnitOfWork
from app.core.errors import ConflictError
from app.domain.workflow.workflow_run_status import WorkflowRunStatus

_LOGGER = structlog.get_logger(__name__)

# Provider output keys that name a produced artifact → the MediaAsset ``kind``.
_MEDIA_REF_KEYS: tuple[tuple[str, str], ...] = (("image_ref", "image"), ("video_ref", "video"))

# Minimal mime → extension map for the (cosmetic) storage-key suffix.
_MIME_EXT: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
}
_DEFAULT_MIME = "application/octet-stream"
_KEY_SAFE = re.compile(r"[^A-Za-z0-9._-]")


@dataclass(frozen=True, slots=True)
class _MediaRef:
    kind: str
    url: str
    provider: str | None
    request_id: str
    step_index: int


@dataclass(frozen=True, slots=True)
class IngestGeneratedMediaResult:
    """Outcome of one ingestion pass over a run."""

    status: str  # "ingested" | "noop"
    registered_media_ids: list[UUID] = field(default_factory=list)
    skipped_existing: int = 0


def _sanitize(value: str) -> str:
    return _KEY_SAFE.sub("_", value)


def _ext_for(mime_type: str | None, url: str) -> str:
    if mime_type and mime_type in _MIME_EXT:
        return _MIME_EXT[mime_type]
    # Fall back to a plausible suffix from the URL path.
    tail = url.split("?", 1)[0].rsplit("/", 1)[-1]
    if "." in tail:
        suffix = "." + tail.rsplit(".", 1)[-1]
        if 2 <= len(suffix) <= 6 and suffix[1:].isalnum():
            return suffix.lower()
    return ""


def _collect_refs(steps: Any) -> list[_MediaRef]:
    """Extract every produced-media reference from a run's succeeded steps' outputs.

    Reads the opaque provider response views the runner already persisted
    (``step.output['provider_outputs'][*] = {provider, request_id, status, output}``).
    Non-media outputs (e.g. LLM text) are skipped. Ordered by ``(step_index,
    request_id)`` for deterministic keys.
    """
    refs: list[_MediaRef] = []
    for step in steps:
        output = getattr(step, "output", None)
        if not isinstance(output, dict):
            continue
        for view in output.get("provider_outputs", []) or []:
            if not isinstance(view, dict):
                continue
            bag = view.get("output")
            bag = bag if isinstance(bag, dict) else {}
            for ref_key, kind in _MEDIA_REF_KEYS:
                url = bag.get(ref_key)
                if isinstance(url, str) and url:
                    refs.append(
                        _MediaRef(
                            kind=kind,
                            url=url,
                            provider=view.get("provider"),
                            request_id=str(view.get("request_id") or ""),
                            step_index=int(getattr(step, "step_index", 0)),
                        )
                    )
    refs.sort(key=lambda r: (r.step_index, r.request_id))
    return refs


class IngestGeneratedMedia:
    """Download + store + register the generated artifacts of a succeeded run."""

    def __init__(
        self,
        uow: IUnitOfWork,
        storage: IObjectStorage,
        downloader: IMediaDownloader,
    ) -> None:
        self._uow = uow
        self._storage = storage
        self._downloader = downloader

    async def execute(
        self, *, project_id: UUID, workflow_run_id: UUID
    ) -> IngestGeneratedMediaResult:
        # Phase 1 — read-only load (run must be terminally succeeded; W8.4.2).
        async with self._uow:
            run = await self._uow.workflow_runs.get_owned(project_id, workflow_run_id)
            if run is None or run.status != WorkflowRunStatus.SUCCEEDED.value:
                return IngestGeneratedMediaResult(status="noop")
            ownership = await self._uow.projects.get_ownership(project_id)
            if ownership is None:
                return IngestGeneratedMediaResult(status="noop")
            steps = await self._uow.workflow_runs.list_steps(workflow_run_id)

        tenant_id, owner_user_id = ownership
        refs = _collect_refs(steps)
        if not refs:
            return IngestGeneratedMediaResult(status="noop")

        registered: list[UUID] = []
        skipped = 0
        for ref in refs:
            # Phase 2 — fetch + store OUTSIDE any DB transaction.
            downloaded = await self._downloader.download(ref.url)
            mime_type = downloaded.mime_type or _DEFAULT_MIME
            key = (
                f"{tenant_id}/{project_id}/{workflow_run_id}/"
                f"{ref.step_index}/{_sanitize(ref.request_id) or ref.kind}"
                f"{_ext_for(downloaded.mime_type, ref.url)}"
            )
            stored = await self._storage.put(
                key=key, data=downloaded.content, content_type=downloaded.mime_type
            )
            checksum = hashlib.sha256(downloaded.content).digest()

            # Phase 3 — register the asset (deterministic-key ConflictError = idempotent).
            async with self._uow:
                try:
                    asset = await self._uow.media.add(
                        tenant_id=tenant_id,
                        owner_user_id=owner_user_id,
                        kind=ref.kind,
                        source="generated",
                        storage_backend=stored.backend,
                        storage_bucket=stored.bucket,
                        storage_key=stored.key,
                        mime_type=mime_type,
                        size_bytes=downloaded.size_bytes,
                        checksum_sha256=checksum,
                        project_id=project_id,
                        scene_id=None,
                        prompt_id=None,
                        model_id=None,
                        provider=ref.provider,
                        width=None,
                        height=None,
                        duration_seconds=None,
                        source_metadata={
                            "workflow_run_id": str(workflow_run_id),
                            "step_index": ref.step_index,
                            "request_id": ref.request_id,
                            "source_url": ref.url,
                        },
                    )
                    await self._uow.commit()
                    registered.append(asset.id)
                except ConflictError:
                    skipped += 1

        _LOGGER.info(
            "media.generated_ingested",
            workflow_run_id=str(workflow_run_id),
            project_id=str(project_id),
            registered=len(registered),
            skipped_existing=skipped,
        )
        return IngestGeneratedMediaResult(
            status="ingested" if registered else "noop",
            registered_media_ids=registered,
            skipped_existing=skipped,
        )
