"""Tests for ``FfmpegThumbnailer`` (Slice α8.4c).

Two tiers:

* **Unit** — input validation + failure mapping that never spawn a real binary
  (negative timestamp, missing binary → ``ThumbnailError``).
* **Opt-in integration** — a real ``ffmpeg`` frame extract + ``ffprobe`` bitrate
  probe, skipped when the binary is unavailable (so CI without FFmpeg stays green).
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from app.application.interfaces.thumbnailer import ThumbnailError
from app.infrastructure.render import FfmpegThumbnailer

pytestmark = pytest.mark.unit

_HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


async def test_negative_timestamp_raises() -> None:
    thumbnailer = FfmpegThumbnailer()
    with pytest.raises(ThumbnailError):
        await thumbnailer.thumbnail(source_path="/x.mp4", at_seconds=-1.0)


async def test_missing_binary_maps_to_thumbnail_error(tmp_path: Path) -> None:
    thumbnailer = FfmpegThumbnailer(ffmpeg_path="/definitely/not/a/real/ffmpeg")
    with pytest.raises(ThumbnailError):
        await thumbnailer.thumbnail(source_path=str(tmp_path / "x.mp4"), at_seconds=0.0)


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")
async def test_real_ffmpeg_thumbnail_roundtrip(tmp_path: Path) -> None:
    src = tmp_path / "src.mp4"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc=duration=2:size=320x240:rate=15",
        "-pix_fmt",
        "yuv420p",
        str(src),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.communicate()
    assert proc.returncode == 0

    thumbnailer = FfmpegThumbnailer(timeout_seconds=120.0)
    thumb = await thumbnailer.thumbnail(source_path=str(src), at_seconds=1.0)

    assert thumb.image and len(thumb.image) > 0
    assert thumb.mime_type == "image/jpeg"
    assert thumb.width == 320 and thumb.height == 240
    assert thumb.source_bitrate is None or thumb.source_bitrate > 0
