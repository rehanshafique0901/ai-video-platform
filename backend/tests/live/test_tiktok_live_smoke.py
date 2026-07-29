"""Opt-in live smoke test for the real TikTok destination (α9.6).

**Excluded from CI.** Stage 24 stays deterministic + network-free (``httpx.MockTransport``).
This is a *manual*, pre-release confidence check that the real adapter actually talks to the
TikTok Content Posting API. It runs only when explicitly enabled:

    TIKTOK_LIVE_SMOKE=1 \\
    TIKTOK_SMOKE_ACCESS_TOKEN=act.<valid-bearer-with-video.publish-scope> \\
    TIKTOK_SMOKE_VIDEO_PATH=/abs/path/to/small.mp4 \\
      python -m pytest -m live_smoke tests/live/test_tiktok_live_smoke.py

It posts with ``PRIVATE`` visibility (``SELF_ONLY``), so nothing becomes public — which is also
the only privacy level an unaudited TikTok sandbox client is permitted to use. Marked
``live_smoke`` (never ``unit``/``integration``), so the CI gate never selects it.
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
from app.infrastructure.publishing.destinations.tiktok import TikTokDestination

_ENABLED = os.getenv("TIKTOK_LIVE_SMOKE") == "1"


@pytest.mark.live_smoke
@pytest.mark.skipif(not _ENABLED, reason="set TIKTOK_LIVE_SMOKE=1 to run the live TikTok smoke")
async def test_live_tiktok_upload_private_video() -> None:
    access_token = os.environ["TIKTOK_SMOKE_ACCESS_TOKEN"]
    video_path = os.environ["TIKTOK_SMOKE_VIDEO_PATH"]
    size = Path(video_path).stat().st_size

    async with httpx.AsyncClient(timeout=120.0) as http:
        dest = TikTokDestination(
            http=http,
            api_base_url="https://open.tiktokapis.com",
            chunk_size_bytes=10_000_000,
            status_poll_interval_seconds=3.0,
            status_poll_budget_seconds=180.0,
        )
        package = build_content_package(
            media_asset_id=uuid4(),
            project_title=f"live-smoke {datetime.now(UTC).isoformat()}",
            visibility=Visibility.PRIVATE,
        )
        result = await dest.publish(
            package=package,
            auth=AuthorizedContext(
                access_token=access_token, expires_at=None, scopes=("video.publish",)
            ),
            media=UploadMedia(path=video_path, mime_type="video/mp4", size_bytes=size),
        )

    # The durable publish_id is the identifier; no post URL is derivable (α9.6 ruling 3).
    assert result.external_post_id
    assert result.post_url is None
