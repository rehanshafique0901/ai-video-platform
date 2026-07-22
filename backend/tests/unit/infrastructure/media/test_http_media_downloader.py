"""Unit tests for the HTTP artifact downloader (Slice α8.4a)."""

from __future__ import annotations

import httpx
import pytest

from app.application.interfaces.media_downloader import MediaDownloadError
from app.infrastructure.media import HttpMediaDownloader

pytestmark = pytest.mark.unit


def _downloader(handler, *, max_bytes: int = 1024) -> HttpMediaDownloader:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return HttpMediaDownloader(client=client, max_bytes=max_bytes)


async def test_download_success_returns_bytes_mime_and_size() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"IMG", headers={"content-type": "image/png"})

    result = await _downloader(handler).download("https://cdn.example/x.png")
    assert result.content == b"IMG"
    assert result.mime_type == "image/png"
    assert result.size_bytes == 3


async def test_download_strips_charset_from_content_type() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"{}", headers={"content-type": "application/json; charset=utf-8"}
        )

    result = await _downloader(handler).download("https://cdn.example/x")
    assert result.mime_type == "application/json"


async def test_download_missing_content_type_is_none() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x")

    result = await _downloader(handler).download("https://cdn.example/x")
    assert result.mime_type is None


async def test_non_200_raises_download_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"nope")

    with pytest.raises(MediaDownloadError):
        await _downloader(handler).download("https://cdn.example/missing")


async def test_size_cap_exceeded_raises() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 50)

    with pytest.raises(MediaDownloadError):
        await _downloader(handler, max_bytes=10).download("https://cdn.example/big")


async def test_transport_error_maps_to_download_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(MediaDownloadError):
        await _downloader(handler).download("https://cdn.example/x")
