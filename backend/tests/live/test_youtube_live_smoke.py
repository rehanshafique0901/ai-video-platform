"""Opt-in live smoke test for the real YouTube destination (α8.6c, EQ4).

**Excluded from CI.** Stage 14 stays deterministic + network-free (the Mock destination is
the CI default). This test is a *manual*, pre-release confidence check that the real adapter
+ OAuth client actually talk to Google. It runs only when explicitly enabled:

    YOUTUBE_LIVE_SMOKE=1 \\
    YOUTUBE_SMOKE_ACCESS_TOKEN=ya29.<valid-bearer-with-youtube.upload-scope> \\
    YOUTUBE_SMOKE_VIDEO_PATH=/abs/path/to/small.mp4 \\
      python -m pytest -m live_smoke tests/live/test_youtube_live_smoke.py

It uploads one **private** ("private" visibility, PUB-11-safe) video, so nothing becomes
public. It is marked ``live_smoke`` (never ``unit``/``integration``), so the CI gate — which
runs only ``-m unit`` (Stage 4) and ``-m integration`` (Stage 14) — never selects it.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.application.interfaces.destination_publisher import UploadMedia
from app.application.interfaces.social_credential_store import AuthorizedContext
from app.domain.publishing.content_package import Visibility, build_content_package
from app.infrastructure.publishing.destinations.youtube import YouTubeDestination

_ENABLED = os.getenv("YOUTUBE_LIVE_SMOKE") == "1"


@pytest.mark.live_smoke
@pytest.mark.skipif(not _ENABLED, reason="set YOUTUBE_LIVE_SMOKE=1 to run the live YouTube smoke")
async def test_live_youtube_upload_private_video() -> None:
    access_token = os.environ["YOUTUBE_SMOKE_ACCESS_TOKEN"]
    video_path = os.environ["YOUTUBE_SMOKE_VIDEO_PATH"]
    size = Path(video_path).stat().st_size

    async with httpx.AsyncClient(timeout=120.0) as http:
        dest = YouTubeDestination(http=http, api_base_url="https://www.googleapis.com")
        package = build_content_package(
            media_asset_id=uuid4(),
            project_title=f"live-smoke {datetime.now(UTC).isoformat()}",
            description="α8.6c live smoke — safe to delete.",
            visibility=Visibility.PRIVATE,
        )
        result = await dest.publish(
            package=package,
            auth=AuthorizedContext(
                access_token=access_token, expires_at=None, scopes=("youtube.upload",)
            ),
            media=UploadMedia(path=video_path, mime_type="video/mp4", size_bytes=size),
        )

    assert result.external_post_id
    assert result.post_url and result.external_post_id in result.post_url
