"""Unit tests for ``YouTubeDestination`` (α8.6c) — network-free via MockTransport.

Covers request/response mapping, the resumable two-step protocol, and the full
error-classification table (§6) including the PUB-11 ambiguous-outcome rule.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.application.interfaces.destination_publisher import DestinationError, UploadMedia
from app.application.interfaces.social_credential_store import AuthorizedContext
from app.domain.publishing.content_package import Visibility, build_content_package
from app.infrastructure.publishing.destinations.youtube import YouTubeDestination

_INITIATE_PATH = "/upload/youtube/v3/videos"
_SESSION_URL = "https://upload.googleapis.com/resumable/session/abc"


def _no_network(_: httpx.Request) -> httpx.Response:  # pragma: no cover - safety net
    raise AssertionError("no HTTP call expected")


def _dest(handler: Callable[[httpx.Request], httpx.Response]) -> YouTubeDestination:
    return YouTubeDestination(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        api_base_url="https://www.googleapis.com",
    )


def _auth(token: str = "ya29.bearer") -> AuthorizedContext:
    return AuthorizedContext(
        access_token=token, expires_at=datetime(2026, 7, 27, tzinfo=UTC), scopes=("upload",)
    )


def _media(tmp_path: Path, *, size: int = 2048) -> UploadMedia:
    artifact = tmp_path / "artifact.mp4"
    artifact.write_bytes(b"x" * size)
    return UploadMedia(path=str(artifact), mime_type="video/mp4", size_bytes=size)


def _initiate_then(put: httpx.Response, *, captured: list[httpx.Request] | None = None):
    """A handler: initiate → 200 + session Location; PUT → the given response."""

    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(request)
        if request.method == "POST" and request.url.path == _INITIATE_PATH:
            return httpx.Response(200, headers={"Location": _SESSION_URL})
        if request.method == "PUT":
            return put
        raise AssertionError(f"unexpected {request.method} {request.url}")

    return handler


@pytest.mark.unit
async def test_publish_happy_path_maps_and_returns_identity(tmp_path: Path) -> None:
    captured: list[httpx.Request] = []
    dest = _dest(_initiate_then(httpx.Response(200, json={"id": "vid_123"}), captured=captured))
    pkg = build_content_package(media_asset_id=uuid4(), project_title="My Project", tags=("a", "b"))
    result = await dest.publish(package=pkg, auth=_auth(), media=_media(tmp_path))

    assert dest.platform == "youtube"
    assert result.external_post_id == "vid_123"
    assert result.post_url == "https://www.youtube.com/watch?v=vid_123"

    initiate = captured[0]
    assert initiate.headers["Authorization"] == "Bearer ya29.bearer"
    assert initiate.headers["X-Upload-Content-Type"] == "video/mp4"
    assert initiate.headers["X-Upload-Content-Length"] == "2048"
    body = json.loads(initiate.content)
    assert body["snippet"]["title"] == "My Project"
    assert body["snippet"]["tags"] == ["a", "b"]
    assert body["status"]["privacyStatus"] == "private"  # default visibility
    assert body["status"]["selfDeclaredMadeForKids"] is False


@pytest.mark.unit
async def test_visibility_and_schedule_mapping(tmp_path: Path) -> None:
    captured: list[httpx.Request] = []
    dest = _dest(_initiate_then(httpx.Response(200, json={"id": "v"}), captured=captured))
    when = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
    pkg = build_content_package(
        media_asset_id=uuid4(),
        project_title="P",
        visibility=Visibility.PUBLIC,
        publish_at=when,
    )
    await dest.publish(package=pkg, auth=_auth(), media=_media(tmp_path))
    body = json.loads(captured[0].content)
    # publish_at forces private + carries publishAt (overrides the requested public).
    assert body["status"]["privacyStatus"] == "private"
    assert body["status"]["publishAt"] == when.isoformat()


@pytest.mark.unit
async def test_public_visibility_without_schedule_maps_directly(tmp_path: Path) -> None:
    captured: list[httpx.Request] = []
    dest = _dest(_initiate_then(httpx.Response(200, json={"id": "v"}), captured=captured))
    pkg = build_content_package(
        media_asset_id=uuid4(), project_title="P", visibility=Visibility.UNLISTED
    )
    await dest.publish(package=pkg, auth=_auth(), media=_media(tmp_path))
    assert json.loads(captured[0].content)["status"]["privacyStatus"] == "unlisted"


@pytest.mark.unit
async def test_missing_bearer_is_permanent(tmp_path: Path) -> None:
    dest = _dest(_initiate_then(httpx.Response(200, json={"id": "v"})))
    pkg = build_content_package(media_asset_id=uuid4(), project_title="P")
    with pytest.raises(DestinationError) as exc:
        await dest.publish(package=pkg, auth=_auth(token=""), media=_media(tmp_path))
    assert exc.value.retryable is False
    assert exc.value.code == "missing_bearer"


@pytest.mark.unit
async def test_empty_media_is_permanent(tmp_path: Path) -> None:
    dest = _dest(_initiate_then(httpx.Response(200, json={"id": "v"})))
    pkg = build_content_package(media_asset_id=uuid4(), project_title="P")
    with pytest.raises(DestinationError) as exc:
        await dest.publish(package=pkg, auth=_auth(), media=_media(tmp_path, size=0))
    assert exc.value.retryable is False
    assert exc.value.code == "empty_media"


@pytest.mark.unit
async def test_title_over_limit_is_permanent_invalid_metadata(tmp_path: Path) -> None:
    dest = _dest(_no_network)
    pkg = build_content_package(media_asset_id=uuid4(), project_title="x" * 101)
    with pytest.raises(DestinationError) as exc:
        await dest.publish(package=pkg, auth=_auth(), media=_media(tmp_path))
    assert exc.value.retryable is False
    assert exc.value.code == "invalid_metadata"


@pytest.mark.unit
@pytest.mark.parametrize(
    "status,code",
    [(400, "invalid_metadata"), (401, "unauthorized"), (403, "forbidden")],
)
async def test_initiate_client_errors_are_permanent(tmp_path: Path, status: int, code: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "x"})

    pkg = build_content_package(media_asset_id=uuid4(), project_title="P")
    with pytest.raises(DestinationError) as exc:
        await _dest(handler).publish(package=pkg, auth=_auth(), media=_media(tmp_path))
    assert exc.value.retryable is False
    assert exc.value.code == code


@pytest.mark.unit
@pytest.mark.parametrize("status", [429, 500, 503])
async def test_initiate_transient_errors_are_retryable(tmp_path: Path, status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status)

    pkg = build_content_package(media_asset_id=uuid4(), project_title="P")
    with pytest.raises(DestinationError) as exc:
        await _dest(handler).publish(package=pkg, auth=_auth(), media=_media(tmp_path))
    assert exc.value.retryable is True
    assert exc.value.code == "youtube_transient"


@pytest.mark.unit
async def test_initiate_missing_location_is_retryable(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)  # no Location header

    pkg = build_content_package(media_asset_id=uuid4(), project_title="P")
    with pytest.raises(DestinationError) as exc:
        await _dest(handler).publish(package=pkg, auth=_auth(), media=_media(tmp_path))
    assert exc.value.retryable is True
    assert exc.value.code == "youtube_transient"


@pytest.mark.unit
async def test_initiate_connect_error_is_retryable(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns")

    pkg = build_content_package(media_asset_id=uuid4(), project_title="P")
    with pytest.raises(DestinationError) as exc:
        await _dest(handler).publish(package=pkg, auth=_auth(), media=_media(tmp_path))
    assert exc.value.retryable is True
    assert exc.value.code == "youtube_transient"


@pytest.mark.unit
@pytest.mark.parametrize("status", [429, 500, 503])
async def test_put_transient_after_transmission_is_permanent_pub11(
    tmp_path: Path, status: int
) -> None:
    # PUB-11: bytes may have been accepted; a 5xx/429 during the PUT is NOT retried.
    dest = _dest(_initiate_then(httpx.Response(status)))
    pkg = build_content_package(media_asset_id=uuid4(), project_title="P")
    with pytest.raises(DestinationError) as exc:
        await dest.publish(package=pkg, auth=_auth(), media=_media(tmp_path))
    assert exc.value.retryable is False
    assert exc.value.code == "ambiguous_upload_outcome"


@pytest.mark.unit
async def test_put_read_timeout_is_permanent_pub11(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, headers={"Location": _SESSION_URL})
        raise httpx.ReadTimeout("lost after bytes")

    pkg = build_content_package(media_asset_id=uuid4(), project_title="P")
    with pytest.raises(DestinationError) as exc:
        await _dest(handler).publish(package=pkg, auth=_auth(), media=_media(tmp_path))
    assert exc.value.retryable is False
    assert exc.value.code == "ambiguous_upload_outcome"


@pytest.mark.unit
async def test_put_connect_error_is_retryable(tmp_path: Path) -> None:
    # A connection that never established sent no bytes → safe to retry.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, headers={"Location": _SESSION_URL})
        raise httpx.ConnectError("refused")

    pkg = build_content_package(media_asset_id=uuid4(), project_title="P")
    with pytest.raises(DestinationError) as exc:
        await _dest(handler).publish(package=pkg, auth=_auth(), media=_media(tmp_path))
    assert exc.value.retryable is True
    assert exc.value.code == "youtube_transient"


@pytest.mark.unit
async def test_put_success_without_video_id_is_permanent_pub11(tmp_path: Path) -> None:
    dest = _dest(_initiate_then(httpx.Response(200, json={"kind": "youtube#video"})))
    pkg = build_content_package(media_asset_id=uuid4(), project_title="P")
    with pytest.raises(DestinationError) as exc:
        await dest.publish(package=pkg, auth=_auth(), media=_media(tmp_path))
    assert exc.value.retryable is False
    assert exc.value.code == "ambiguous_upload_outcome"
