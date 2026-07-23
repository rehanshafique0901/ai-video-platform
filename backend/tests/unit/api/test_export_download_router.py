"""Unit tests for the α8.5b.1 export-download endpoint (HTTP contract in isolation).

Mounts only the export-jobs router on a bare FastAPI app + the app error handlers, and
overrides the ``DownloadExport`` use case + the auth seam — no DB, no container init. Pins:

* 200 — a byte stream with ``Content-Disposition: attachment`` + ``Content-Type``.
* 302 — a ``RedirectDelivery`` (the α8.5b.2 cloud shape) is rendered as a redirect.
* 404 / 409 — ``NotFoundError`` / ``ConflictError`` map through the standard error envelope.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.v1.deps import get_current_user
from app.api.v1.routers import export_jobs
from app.application.interfaces.download_delivery import (
    DeliveryDecision,
    RedirectDelivery,
    StreamDelivery,
)
from app.core import container
from app.core.errors import ConflictError, NotFoundError, register_exception_handlers

pytestmark = pytest.mark.unit


class _StubDownload:
    def __init__(
        self, *, decision: DeliveryDecision | None = None, raises: Exception | None = None
    ) -> None:
        self._decision = decision
        self._raises = raises

    async def execute(self, **_kwargs: object) -> DeliveryDecision:
        if self._raises is not None:
            raise self._raises
        assert self._decision is not None
        return self._decision


def _app(stub: _StubDownload) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(export_jobs.router, prefix="/api/v1")
    app.dependency_overrides[container.get_download_export_use_case] = lambda: stub
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=uuid4(), tenant_id=uuid4()
    )
    return app


def _url() -> str:
    return f"/api/v1/projects/{uuid4()}/render-jobs/{uuid4()}/exports/{uuid4()}/download"


async def _get(app: FastAPI, path: str):  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get(path)


async def test_stream_download_returns_bytes_and_attachment_header() -> None:
    payload = b"FINISHED-VIDEO"

    async def _chunks() -> AsyncIterator[bytes]:
        yield payload

    stub = _StubDownload(
        decision=StreamDelivery(
            chunks=_chunks(),
            media_type="video/mp4",
            filename="export_hd_1080p_horizontal.mp4",
            content_length=len(payload),
        )
    )
    response = await _get(_app(stub), _url())

    assert response.status_code == 200
    assert response.content == payload
    assert response.headers["content-type"].startswith("video/mp4")
    assert (
        response.headers["content-disposition"]
        == 'attachment; filename="export_hd_1080p_horizontal.mp4"'
    )


async def test_redirect_delivery_returns_302() -> None:
    stub = _StubDownload(
        decision=RedirectDelivery(url="https://cdn.example/signed", expires_at=None)
    )
    transport = ASGITransport(app=_app(stub))
    async with AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:  # no follow_redirects
        response = await client.get(_url())

    assert response.status_code == 302
    assert response.headers["location"] == "https://cdn.example/signed"


async def test_not_found_maps_to_404() -> None:
    stub = _StubDownload(raises=NotFoundError("export job not found"))
    response = await _get(_app(stub), _url())
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


async def test_not_ready_maps_to_409() -> None:
    stub = _StubDownload(raises=ConflictError("export is not ready for download"))
    response = await _get(_app(stub), _url())
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONFLICT"
