"""Tests for the ffmpeg slideshow renderer (unit validation + skipif integration)."""

from __future__ import annotations

import shutil
from io import BytesIO

import pytest
from PIL import Image

from app.application.interfaces.slideshow_renderer import (
    SlideshowFrame,
    SlideshowRenderError,
    SlideshowSpec,
)
from app.infrastructure.render.ffmpeg_slideshow_renderer import FfmpegSlideshowRenderer
from app.infrastructure.render.ffprobe_video_probe import FfprobeVideoProbe

pytestmark = pytest.mark.unit

_HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _png(colour: tuple[int, int, int], size: tuple[int, int] = (160, 120)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, colour).save(buf, format="PNG")
    return buf.getvalue()


def _frames(*colours: tuple[int, int, int], duration: float = 1.0) -> tuple[SlideshowFrame, ...]:
    return tuple(SlideshowFrame(data=_png(c), duration_seconds=duration) for c in colours)


async def test_no_frames_raises() -> None:
    with pytest.raises(SlideshowRenderError):
        await FfmpegSlideshowRenderer().render(frames=(), spec=SlideshowSpec(width=320, height=240))


async def test_zero_duration_raises() -> None:
    frames = (SlideshowFrame(data=_png((0, 0, 0)), duration_seconds=0.0),)
    with pytest.raises(SlideshowRenderError):
        await FfmpegSlideshowRenderer().render(frames=frames, spec=SlideshowSpec(320, 240))


async def test_missing_binary_raises() -> None:
    renderer = FfmpegSlideshowRenderer(ffmpeg_path="definitely-not-ffmpeg")
    with pytest.raises(SlideshowRenderError):
        await renderer.render(frames=_frames((1, 2, 3)), spec=SlideshowSpec(320, 240))


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")
async def test_renders_hard_cut_video_with_expected_duration_and_dims() -> None:
    renderer = FfmpegSlideshowRenderer()
    frames = _frames((200, 30, 30), (30, 200, 30), (30, 30, 200), duration=1.0)
    spec = SlideshowSpec(width=320, height=240, fps=10)

    video = await renderer.render(frames=frames, spec=spec)
    assert video.data[:4] != b"" and video.size_bytes > 0

    observed = await FfprobeVideoProbe().probe(video.data)
    assert observed.width == 320 and observed.height == 240
    assert observed.duration_seconds is not None
    assert observed.duration_seconds == pytest.approx(3.0, abs=0.5)


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")
async def test_crossfade_shortens_total_duration() -> None:
    renderer = FfmpegSlideshowRenderer()
    frames = _frames((200, 30, 30), (30, 200, 30), (30, 30, 200), duration=1.0)
    spec = SlideshowSpec(width=320, height=240, fps=10, crossfade_seconds=0.5)

    video = await renderer.render(frames=frames, spec=spec)
    observed = await FfprobeVideoProbe().probe(video.data)
    # 3x1s with 0.5s crossfade between each of the 2 joins => ~2.0s total.
    assert observed.duration_seconds is not None
    assert observed.duration_seconds == pytest.approx(2.0, abs=0.5)
