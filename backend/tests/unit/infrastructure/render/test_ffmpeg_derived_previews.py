"""Tests for the α8.4d derived-preview FFmpeg adapters.

Two tiers per adapter:

* **Unit** — input validation + launch-failure mapping (no real binary).
* **Opt-in integration** — real ``ffmpeg`` roundtrips, skipped when the binary is
  unavailable (α8.4b/α8.4c pattern). The waveform integration test also covers the
  data-dependent *not-applicable* path (silent source → ``None``).
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from app.application.interfaces.gif_previewer import GifPreviewError
from app.application.interfaces.preview_clipper import PreviewClipError
from app.application.interfaces.waveform_renderer import WaveformError
from app.infrastructure.render import (
    FfmpegGifPreviewer,
    FfmpegPreviewClipper,
    FfmpegWaveformRenderer,
)

pytestmark = pytest.mark.unit

_HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


async def _make_video(path: Path, *, with_audio: bool) -> None:
    args = ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=15"]
    if with_audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=440:duration=2"]
    args += ["-pix_fmt", "yuv420p", "-shortest", str(path)]
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    await proc.communicate()
    assert proc.returncode == 0


# --- preview clipper ------------------------------------------------------


async def test_preview_invalid_args_raise() -> None:
    clipper = FfmpegPreviewClipper()
    with pytest.raises(PreviewClipError):
        await clipper.preview(source_path="/x.mp4", max_seconds=0, max_width=640)
    with pytest.raises(PreviewClipError):
        await clipper.preview(source_path="/x.mp4", max_seconds=5, max_width=0)


async def test_preview_missing_binary_maps_to_error(tmp_path: Path) -> None:
    clipper = FfmpegPreviewClipper(ffmpeg_path="/definitely/not/ffmpeg")
    with pytest.raises(PreviewClipError):
        await clipper.preview(source_path=str(tmp_path / "x.mp4"), max_seconds=5, max_width=640)


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")
async def test_preview_real_roundtrip(tmp_path: Path) -> None:
    src = tmp_path / "src.mp4"
    await _make_video(src, with_audio=True)
    clip = await FfmpegPreviewClipper(timeout_seconds=120.0).preview(
        source_path=str(src), max_seconds=1.0, max_width=160
    )
    assert clip.data and clip.mime_type == "video/mp4"
    assert clip.width is not None and clip.width <= 160


# --- gif previewer --------------------------------------------------------


async def test_gif_invalid_args_raise() -> None:
    previewer = FfmpegGifPreviewer()
    with pytest.raises(GifPreviewError):
        await previewer.gif(source_path="/x.mp4", max_seconds=0, fps=10, max_width=480)
    with pytest.raises(GifPreviewError):
        await previewer.gif(source_path="/x.mp4", max_seconds=3, fps=0, max_width=480)


async def test_gif_missing_binary_maps_to_error(tmp_path: Path) -> None:
    previewer = FfmpegGifPreviewer(ffmpeg_path="/definitely/not/ffmpeg")
    with pytest.raises(GifPreviewError):
        await previewer.gif(
            source_path=str(tmp_path / "x.mp4"), max_seconds=3, fps=10, max_width=480
        )


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")
async def test_gif_real_roundtrip(tmp_path: Path) -> None:
    src = tmp_path / "src.mp4"
    await _make_video(src, with_audio=False)
    gif = await FfmpegGifPreviewer(timeout_seconds=120.0).gif(
        source_path=str(src), max_seconds=1.0, fps=8, max_width=120
    )
    assert gif.data and gif.mime_type == "image/gif"


# --- waveform renderer ----------------------------------------------------


async def test_waveform_invalid_args_raise() -> None:
    renderer = FfmpegWaveformRenderer()
    with pytest.raises(WaveformError):
        await renderer.waveform(source_path="/x.mp4", width=0, height=120)


async def test_waveform_missing_binary_maps_to_error(tmp_path: Path) -> None:
    renderer = FfmpegWaveformRenderer(ffprobe_path="/definitely/not/ffprobe")
    with pytest.raises(WaveformError):
        await renderer.waveform(source_path=str(tmp_path / "x.mp4"), width=640, height=120)


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")
async def test_waveform_real_roundtrip_with_audio(tmp_path: Path) -> None:
    src = tmp_path / "src.mp4"
    await _make_video(src, with_audio=True)
    wave = await FfmpegWaveformRenderer(timeout_seconds=120.0).waveform(
        source_path=str(src), width=320, height=80
    )
    assert wave is not None
    assert wave.data and wave.mime_type == "image/png"


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")
async def test_waveform_silent_source_is_not_applicable(tmp_path: Path) -> None:
    src = tmp_path / "silent.mp4"
    await _make_video(src, with_audio=False)
    wave = await FfmpegWaveformRenderer(timeout_seconds=120.0).waveform(
        source_path=str(src), width=320, height=80
    )
    assert wave is None  # no audio stream → not applicable, not a failure
