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

from app.application.interfaces.renderer import (
    AudioInput,
    RenderError,
    RenderInput,
    RenderSpec,
)
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


async def test_non_positive_audio_trim_window_raises(tmp_path: Path) -> None:
    # α8.4e: audio trim windows are validated before any binary is spawned.
    renderer = FfmpegRenderer()
    spec = RenderSpec(
        inputs=(RenderInput(path="/x.mp4", source_start_seconds=0.0, source_end_seconds=1.0),),
        output_path=str(tmp_path / "o.mp4"),
        audio_inputs=(
            AudioInput(
                path="/a.mp3",
                source_start_seconds=1.0,
                source_end_seconds=1.0,
                start_seconds=0.0,
            ),
        ),
    )
    with pytest.raises(RenderError):
        await renderer.render(spec)


async def test_negative_audio_start_raises(tmp_path: Path) -> None:
    renderer = FfmpegRenderer()
    spec = RenderSpec(
        inputs=(RenderInput(path="/x.mp4", source_start_seconds=0.0, source_end_seconds=1.0),),
        output_path=str(tmp_path / "o.mp4"),
        audio_inputs=(
            AudioInput(
                path="/a.mp3",
                source_start_seconds=0.0,
                source_end_seconds=1.0,
                start_seconds=-1.0,
            ),
        ),
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


async def _probe_has_audio_stream(path: Path) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "quiet",
        "-select_streams",
        "a",
        "-show_entries",
        "stream=index",
        "-of",
        "csv=p=0",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    return bool(stdout.decode().strip())


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")
async def test_real_ffmpeg_mixes_audio(tmp_path: Path) -> None:
    # α8.4e: a video clip with audio + a dedicated audio track → output has audio.
    async def _make_video_with_audio(path: Path) -> None:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=2:size=320x240:rate=15",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-pix_fmt",
            "yuv420p",
            "-shortest",
            str(path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
        assert proc.returncode == 0

    async def _make_audio(path: Path) -> None:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:duration=2",
            str(path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
        assert proc.returncode == 0

    src_video = tmp_path / "v.mp4"
    src_music = tmp_path / "m.mp3"
    await _make_video_with_audio(src_video)
    await _make_audio(src_music)

    renderer = FfmpegRenderer(timeout_seconds=120.0)
    out = tmp_path / "mixed.mp4"
    result = await renderer.render(
        RenderSpec(
            inputs=(
                RenderInput(
                    path=str(src_video),
                    source_start_seconds=0.0,
                    source_end_seconds=2.0,
                    volume=1.0,
                ),
            ),
            output_path=str(out),
            audio_inputs=(
                AudioInput(
                    path=str(src_music),
                    source_start_seconds=0.0,
                    source_end_seconds=2.0,
                    start_seconds=0.5,
                    volume=0.5,
                ),
            ),
        )
    )

    assert out.exists()
    assert result.size_bytes > 0
    assert await _probe_has_audio_stream(out)


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")
async def test_real_ffmpeg_silent_timeline_has_no_audio(tmp_path: Path) -> None:
    # Fork F: no authored audio anywhere → silent video (no audio stream), as α8.4b.
    async def _make_silent_video(path: Path) -> None:
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

    src = tmp_path / "s.mp4"
    await _make_silent_video(src)

    renderer = FfmpegRenderer(timeout_seconds=120.0)
    out = tmp_path / "silent.mp4"
    await renderer.render(
        RenderSpec(
            inputs=(RenderInput(path=str(src), source_start_seconds=0.0, source_end_seconds=1.0),),
            output_path=str(out),
        )
    )

    assert out.exists()
    assert not await _probe_has_audio_stream(out)
