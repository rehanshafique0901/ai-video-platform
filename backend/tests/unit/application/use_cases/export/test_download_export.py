"""Unit tests for ``DownloadExport`` (Slice α8.5b.1 — download serving).

Covers the owner-facing read path: success (stream + best-effort accounting), anti-enumeration
404s (foreign user / wrong render job / missing export), readiness guards (409), a vanished
artifact (404), unavailable bytes (404), and the W8.5b.3 guarantee that an accounting failure
never fails the download. The happy path runs the real ``LocalStreamDelivery`` over the fake
object storage — proving a pure byte transfer with no encoder/transcoder involved.
"""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest

from app.application.interfaces.download_delivery import (
    DownloadDeliveryError,
    DownloadRequest,
    IDownloadDelivery,
    RedirectDelivery,
    StreamDelivery,
)
from app.application.use_cases.export.download_export import DownloadExport
from app.core.errors import ConflictError, NotFoundError
from app.infrastructure.delivery.local_stream_delivery import LocalStreamDelivery
from tests.unit.application.use_cases.export._helpers import ExportFixture

pytestmark = pytest.mark.unit


async def _drain(stream: StreamDelivery) -> bytes:
    return b"".join([chunk async for chunk in stream.chunks])


async def test_download_streams_bytes_and_counts() -> None:
    fx = ExportFixture()
    render_job_id, _ = await fx.seed_ready()
    payload = b"THE-FINISHED-VIDEO-BYTES"
    delivery_id = await fx.seed_delivery_asset(data=payload)
    export_id = await fx.seed_succeeded_export(
        render_job_id=render_job_id, delivery_id=delivery_id, file_size_bytes=len(payload)
    )

    uc = DownloadExport(uow=fx.uow, delivery=LocalStreamDelivery(fx.storage, chunk_size=8))
    decision = await uc.execute(
        project_id=fx.project_id,
        render_job_id=render_job_id,
        export_job_id=export_id,
        owner_user_id=fx.owner,
        tenant_id=fx.tenant,
    )

    assert isinstance(decision, StreamDelivery)
    # Pure transfer (W8.5b.2): delivered bytes are byte-identical to what was stored.
    assert await _drain(decision) == payload
    assert decision.media_type == "video/mp4"
    assert decision.filename == "export_hd_1080p_horizontal.mp4"
    assert decision.content_length == len(payload)

    # Best-effort accounting bumped the canonical export row (Fork B).
    job = fx.exports._jobs[export_id]
    assert job.download_count == 1
    assert job.last_downloaded_at is not None


async def test_foreign_user_gets_404() -> None:
    fx = ExportFixture()
    render_job_id, _ = await fx.seed_ready()
    delivery_id = await fx.seed_delivery_asset()
    export_id = await fx.seed_succeeded_export(render_job_id=render_job_id, delivery_id=delivery_id)

    uc = DownloadExport(uow=fx.uow, delivery=LocalStreamDelivery(fx.storage))
    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=fx.project_id,
            render_job_id=render_job_id,
            export_job_id=export_id,
            owner_user_id=uuid4(),  # not the owner
            tenant_id=uuid4(),
        )


async def test_wrong_render_job_gets_404() -> None:
    fx = ExportFixture()
    render_job_id, _ = await fx.seed_ready()
    delivery_id = await fx.seed_delivery_asset()
    export_id = await fx.seed_succeeded_export(render_job_id=render_job_id, delivery_id=delivery_id)

    uc = DownloadExport(uow=fx.uow, delivery=LocalStreamDelivery(fx.storage))
    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=fx.project_id,
            render_job_id=uuid4(),  # export belongs to a different render job
            export_job_id=export_id,
            owner_user_id=fx.owner,
            tenant_id=fx.tenant,
        )


async def test_missing_export_gets_404() -> None:
    fx = ExportFixture()
    render_job_id, _ = await fx.seed_ready()

    uc = DownloadExport(uow=fx.uow, delivery=LocalStreamDelivery(fx.storage))
    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=fx.project_id,
            render_job_id=render_job_id,
            export_job_id=uuid4(),
            owner_user_id=fx.owner,
            tenant_id=fx.tenant,
        )


async def test_not_succeeded_export_gets_409() -> None:
    fx = ExportFixture()
    render_job_id, _ = await fx.seed_ready()
    job = await fx.exports.add(
        render_job_id=render_job_id,
        requested_by_user_id=fx.owner,
        format="mp4",
        quality="hd_1080p",
        orientation="horizontal",
        status="queued",
    )

    uc = DownloadExport(uow=fx.uow, delivery=LocalStreamDelivery(fx.storage))
    with pytest.raises(ConflictError):
        await uc.execute(
            project_id=fx.project_id,
            render_job_id=render_job_id,
            export_job_id=job.id,
            owner_user_id=fx.owner,
            tenant_id=fx.tenant,
        )


async def test_succeeded_without_artifact_gets_409() -> None:
    fx = ExportFixture()
    render_job_id, _ = await fx.seed_ready()
    delivery_id = await fx.seed_delivery_asset()
    export_id = await fx.seed_succeeded_export(render_job_id=render_job_id, delivery_id=delivery_id)
    # Force the pathological "succeeded but no artifact id" shape.
    fx.exports._jobs[export_id] = replace(fx.exports._jobs[export_id], output_media_asset_id=None)

    uc = DownloadExport(uow=fx.uow, delivery=LocalStreamDelivery(fx.storage))
    with pytest.raises(ConflictError):
        await uc.execute(
            project_id=fx.project_id,
            render_job_id=render_job_id,
            export_job_id=export_id,
            owner_user_id=fx.owner,
            tenant_id=fx.tenant,
        )


async def test_vanished_artifact_gets_404() -> None:
    fx = ExportFixture()
    render_job_id, _ = await fx.seed_ready()
    # Point the succeeded export at a delivery asset that does not exist in the media repo.
    export_id = await fx.seed_succeeded_export(render_job_id=render_job_id, delivery_id=uuid4())

    uc = DownloadExport(uow=fx.uow, delivery=LocalStreamDelivery(fx.storage))
    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=fx.project_id,
            render_job_id=render_job_id,
            export_job_id=export_id,
            owner_user_id=fx.owner,
            tenant_id=fx.tenant,
        )
    # A failed lookup is never counted (W8.5b.3).
    assert fx.exports._jobs[export_id].download_count == 0


async def test_unavailable_bytes_gets_404_and_not_counted() -> None:
    fx = ExportFixture()
    render_job_id, _ = await fx.seed_ready()
    # Register a delivery asset whose storage object was never written.
    delivery_id = await fx.seed_delivery_asset(key="exports/missing.mp4")
    del fx.storage.objects["exports/missing.mp4"]
    export_id = await fx.seed_succeeded_export(render_job_id=render_job_id, delivery_id=delivery_id)

    uc = DownloadExport(uow=fx.uow, delivery=LocalStreamDelivery(fx.storage))
    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=fx.project_id,
            render_job_id=render_job_id,
            export_job_id=export_id,
            owner_user_id=fx.owner,
            tenant_id=fx.tenant,
        )
    assert fx.exports._jobs[export_id].download_count == 0


class _RaisingDelivery(IDownloadDelivery):
    """Delivery adapter that always fails to prepare — exercises use-case error mapping."""

    async def deliver(self, request: DownloadRequest) -> StreamDelivery:
        raise DownloadDeliveryError("cannot serve from local adapter")


async def test_delivery_error_maps_to_404() -> None:
    fx = ExportFixture()
    render_job_id, _ = await fx.seed_ready()
    delivery_id = await fx.seed_delivery_asset()
    export_id = await fx.seed_succeeded_export(render_job_id=render_job_id, delivery_id=delivery_id)

    uc = DownloadExport(uow=fx.uow, delivery=_RaisingDelivery())
    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=fx.project_id,
            render_job_id=render_job_id,
            export_job_id=export_id,
            owner_user_id=fx.owner,
            tenant_id=fx.tenant,
        )


async def test_accounting_failure_does_not_fail_download() -> None:
    fx = ExportFixture()
    render_job_id, _ = await fx.seed_ready()
    payload = b"STILL-DELIVERED"
    delivery_id = await fx.seed_delivery_asset(data=payload)
    export_id = await fx.seed_succeeded_export(
        render_job_id=render_job_id, delivery_id=delivery_id, file_size_bytes=len(payload)
    )

    async def _boom(export_job_id: object) -> None:
        raise RuntimeError("counter store is down")

    fx.exports.record_download = _boom  # type: ignore[method-assign]

    uc = DownloadExport(uow=fx.uow, delivery=LocalStreamDelivery(fx.storage))
    # W8.5b.3: the download still succeeds despite the accounting store failing.
    decision = await uc.execute(
        project_id=fx.project_id,
        render_job_id=render_job_id,
        export_job_id=export_id,
        owner_user_id=fx.owner,
        tenant_id=fx.tenant,
    )
    assert isinstance(decision, StreamDelivery)
    assert await _drain(decision) == payload


async def test_redirect_delivery_is_passed_through() -> None:
    """A cloud adapter (α8.5b.2) may return a RedirectDelivery — the use case passes it back."""
    fx = ExportFixture()
    render_job_id, _ = await fx.seed_ready()
    delivery_id = await fx.seed_delivery_asset()
    export_id = await fx.seed_succeeded_export(render_job_id=render_job_id, delivery_id=delivery_id)

    class _RedirectDelivery(IDownloadDelivery):
        async def deliver(self, request: DownloadRequest) -> RedirectDelivery:
            return RedirectDelivery(url="https://cdn.example/signed", expires_at=None)

    uc = DownloadExport(uow=fx.uow, delivery=_RedirectDelivery())
    decision = await uc.execute(
        project_id=fx.project_id,
        render_job_id=render_job_id,
        export_job_id=export_id,
        owner_user_id=fx.owner,
        tenant_id=fx.tenant,
    )
    assert isinstance(decision, RedirectDelivery)
    assert decision.url == "https://cdn.example/signed"
    # Redirect deliveries still count as a download initiation.
    assert fx.exports._jobs[export_id].download_count == 1


async def test_local_delivery_rejects_foreign_backend() -> None:
    """LocalStreamDelivery refuses an artifact stored in a non-local backend (→ 404 upstream)."""
    fx = ExportFixture()
    render_job_id, _ = await fx.seed_ready()
    delivery_id = await fx.seed_delivery_asset()
    # Rewrite the delivery asset to claim a cloud backend the local adapter cannot serve.
    asset = fx.media._media[delivery_id]
    fx.media._media[delivery_id] = replace(asset, storage_backend="s3")
    export_id = await fx.seed_succeeded_export(render_job_id=render_job_id, delivery_id=delivery_id)

    uc = DownloadExport(uow=fx.uow, delivery=LocalStreamDelivery(fx.storage))
    with pytest.raises(NotFoundError):
        await uc.execute(
            project_id=fx.project_id,
            render_job_id=render_job_id,
            export_job_id=export_id,
            owner_user_id=fx.owner,
            tenant_id=fx.tenant,
        )
