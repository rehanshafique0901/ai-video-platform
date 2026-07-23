"""Tests for ``FfmpegRenderer`` (Slice α8.4b).

Two tiers:

* **Unit** — input-validation + failure mapping that never spawn a real binary
  (empty inputs, bad trim window, non-zero exit → ``RenderError``).
* **Opt-in integration** — a real ``ffmpeg`` concat + ``ffprobe`` roundtrip,
  skipped when the binary is unavailable (so CI without FFmpeg stays green).
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from app.application.interfaces.renderer import RenderError, RenderInput, RenderSpec
from app.infrastructure.render import FfmpegRenderer

pytestmark = pytest.mark.unit

_HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


async def test_empty_inputs_raise_render_error(tmp_path: Path) -> None:
    renderer = FfmpegRenderer()
    with pytest.raises(RenderError):
        await renderer.render(RenderSpec(inputs=(), output_path=str(tmp_path / "o.mp4")))


async def test_non_positive_trim_window_raises(tmp_path: Path) -> None:
    renderer = FfmpegRenderer()
    spec = RenderSpec(
        inputs=(
            RenderInput(path="/nonexistent.mp4", source_start_seconds=2.0, source_end_seconds=1.0),
        ),
        output_path=str(tmp_path / "o.mp4"),
    )
    with pytest.raises(RenderError):
        await renderer.render(spec)


async def test_missing_binary_maps_to_render_error(tmp_path: Path) -> None:
    # A binary path that does not exist → launch failure → RenderError (not OSError).
    renderer = FfmpegRenderer(ffmpeg_path="/definitely/not/a/real/ffmpeg")
    spec = RenderSpec(
        inputs=(RenderInput(path="/x.mp4", source_start_seconds=0.0, source_end_seconds=1.0),),
        output_path=str(tmp_path / "o.mp4"),
    )
    with pytest.raises(RenderError):
        await renderer.render(spec)


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")
async def test_real_ffmpeg_concat_roundtrip(tmp_path: Path) -> None:
    # Generate two tiny 1s test sources with ffmpeg's lavfi testsrc.
    async def _make(path: Path) -> None:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=1:size=320x240:rate=15",
            "-pix_fmt",
            "yuv420p",
            str(path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
        assert proc.returncode == 0

    src_a, src_b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    await _make(src_a)
    await _make(src_b)

    renderer = FfmpegRenderer(timeout_seconds=120.0)
    out = tmp_path / "out.mp4"
    result = await renderer.render(
        RenderSpec(
            inputs=(
                RenderInput(path=str(src_a), source_start_seconds=0.0, source_end_seconds=1.0),
                RenderInput(path=str(src_b), source_start_seconds=0.0, source_end_seconds=1.0),
            ),
            output_path=str(out),
        )
    )

    assert out.exists()
    assert result.size_bytes > 0
    assert result.width == 320 and result.height == 240
    assert result.duration_seconds is not None and result.duration_seconds > 1.0
    assert result.codec is not None
