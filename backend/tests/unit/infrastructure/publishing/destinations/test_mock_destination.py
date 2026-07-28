"""Unit tests for ``MockDestination`` + ``DestinationRegistry`` (α8.6b)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.application.interfaces.destination_publisher import (
    DestinationError,
    UploadMedia,
    UploadThumbnail,
)
from app.application.interfaces.social_credential_store import AuthorizedContext
from app.domain.publishing.content_package import build_content_package
from app.infrastructure.publishing.destinations.mock_destination import MockDestination
from app.infrastructure.publishing.destinations.registry import DestinationRegistry


def _auth(token: str = "bearer-xyz") -> AuthorizedContext:
    return AuthorizedContext(
        access_token=token, expires_at=datetime(2026, 7, 27, tzinfo=UTC), scopes=("publish",)
    )


def _media(size: int = 1024, *, thumbnail: UploadThumbnail | None = None) -> UploadMedia:
    return UploadMedia(
        path="/tmp/artifact", mime_type="video/mp4", size_bytes=size, thumbnail=thumbnail
    )


@pytest.mark.unit
async def test_publish_is_deterministic() -> None:
    dest = MockDestination()
    media_id = uuid4()
    pkg = build_content_package(media_asset_id=media_id, project_title="P")
    r1 = await dest.publish(package=pkg, auth=_auth(), media=_media())
    r2 = await dest.publish(package=pkg, auth=_auth(), media=_media())
    assert r1 == r2
    assert r1.external_post_id == f"mock-post-{media_id.hex[:12]}"
    assert r1.post_url is not None and r1.external_post_id in r1.post_url
    assert dest.platform == "mock"


@pytest.mark.unit
async def test_publish_with_thumbnail_is_deterministic_and_ignores_identity() -> None:
    # α9.3 — a thumbnail is best-effort and must not change the returned post identity.
    dest = MockDestination()
    media_id = uuid4()
    pkg = build_content_package(media_asset_id=media_id, project_title="P")
    thumb = UploadThumbnail(path="/tmp/thumb.jpg", mime_type="image/jpeg", size_bytes=512)
    with_thumb = await dest.publish(package=pkg, auth=_auth(), media=_media(thumbnail=thumb))
    without_thumb = await dest.publish(package=pkg, auth=_auth(), media=_media())
    assert with_thumb == without_thumb
    assert with_thumb.external_post_id == f"mock-post-{media_id.hex[:12]}"


@pytest.mark.unit
async def test_publish_rejects_missing_bearer_permanently() -> None:
    dest = MockDestination()
    pkg = build_content_package(media_asset_id=uuid4(), project_title="P")
    with pytest.raises(DestinationError) as exc:
        await dest.publish(package=pkg, auth=_auth(token=""), media=_media())
    assert exc.value.retryable is False


@pytest.mark.unit
async def test_publish_rejects_empty_media_permanently() -> None:
    dest = MockDestination()
    pkg = build_content_package(media_asset_id=uuid4(), project_title="P")
    with pytest.raises(DestinationError) as exc:
        await dest.publish(package=pkg, auth=_auth(), media=_media(size=0))
    assert exc.value.retryable is False


@pytest.mark.unit
def test_registry_resolves_known_platform() -> None:
    dest = MockDestination()
    registry = DestinationRegistry({"mock": dest})
    assert registry.for_platform("mock") is dest
    assert registry.supported_platforms() == frozenset({"mock"})


@pytest.mark.unit
def test_registry_unknown_platform_is_permanent_error() -> None:
    registry = DestinationRegistry({"mock": MockDestination()})
    with pytest.raises(DestinationError) as exc:
        registry.for_platform("youtube")
    assert exc.value.retryable is False
    assert exc.value.code == "unsupported_destination"
