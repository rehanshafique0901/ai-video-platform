"""Tests for the ffprobe video probe (skipif integration + unit for bad input)."""

from __future__ import annotations

import shutil
from io import BytesIO

import pytest
from PIL import Image

from app.application.interfaces.slideshow_renderer import SlideshowFrame, SlideshowSpec
from app.infrastructure.render.ffmpeg_slideshow_renderer import FfmpegSlideshowRenderer
from app.infrastructure.render.ffprobe_video_probe import FfprobeVideoProbe

pytestmark = pytest.mark.unit

_HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _png(colour: tuple[int, int, int]) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (160, 120), colour).save(buf, format="PNG")
    return buf.getvalue()


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")
async def test_probe_measures_real_video() -> None:
    frames = (
        SlideshowFrame(data=_png((10, 10, 10)), duration_seconds=1.0),
        SlideshowFrame(data=_png((240, 240, 240)), duration_seconds=1.0),
    )
    video = await FfmpegSlideshowRenderer().render(
        frames=frames, spec=SlideshowSpec(width=256, height=256, fps=10)
    )
    observed = await FfprobeVideoProbe().probe(video.data)
    assert observed.width == 256 and observed.height == 256
    assert observed.duration_seconds == pytest.approx(2.0, abs=0.5)


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")
async def test_probe_of_garbage_returns_unmeasured() -> None:
    observed = await FfprobeVideoProbe().probe(b"not a video")
    assert observed.duration_seconds is None
    assert observed.width is None and observed.height is None
