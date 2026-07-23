"""Tests for ``FfmpegExporter`` (Slice α8.5a).

Two tiers:

* **Unit** — parameter validation + failure mapping + the resolution-box math that never
  spawn a real binary (bad enum values / missing source → ``ExportError``; ``_target_box``).
* **Opt-in integration** — a real ``ffmpeg`` transcode + ``ffprobe`` roundtrip (mp4 + gif),
  skipped when the binary is unavailable (so CI without FFmpeg stays green).
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from app.application.interfaces.exporter import ExportError, ExportSpec
from app.infrastructure.export import FfmpegExporter
from app.infrastructure.export.ffmpeg_exporter import FfmpegExporter as _Exporter

pytestmark = pytest.mark.unit

_HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


async def test_unsupported_format_raises(tmp_path: Path) -> None:
    exporter = FfmpegExporter()
    src = tmp_path / "master.mp4"
    src.write_bytes(b"x")
    spec = ExportSpec(
        source_path=str(src),
        output_path=str(tmp_path / "o.avi"),
        format="avi",
        quality="hd_1080p",
        orientation="horizontal",
    )
    with pytest.raises(ExportError):
        await exporter.export(spec)


async def test_unsupported_quality_raises(tmp_path: Path) -> None:
    exporter = FfmpegExporter()
    src = tmp_path / "master.mp4"
    src.write_bytes(b"x")
    spec = ExportSpec(
        source_path=str(src),
        output_path=str(tmp_path / "o.mp4"),
        format="mp4",
        quality="8k",
        orientation="horizontal",
    )
    with pytest.raises(ExportError):
        await exporter.export(spec)


async def test_missing_source_raises(tmp_path: Path) -> None:
    exporter = FfmpegExporter()
    spec = ExportSpec(
        source_path=str(tmp_path / "nope.mp4"),
        output_path=str(tmp_path / "o.mp4"),
        format="mp4",
        quality="hd_1080p",
        orientation="horizontal",
    )
    with pytest.raises(ExportError):
        await exporter.export(spec)


async def test_missing_binary_maps_to_export_error(tmp_path: Path) -> None:
    src = tmp_path / "master.mp4"
    src.write_bytes(b"x")
    exporter = FfmpegExporter(ffmpeg_path="/definitely/not/a/real/ffmpeg")
    spec = ExportSpec(
        source_path=str(src),
        output_path=str(tmp_path / "o.mp4"),
        format="mp4",
        quality="hd_1080p",
        orientation="horizontal",
    )
    with pytest.raises(ExportError):
        await exporter.export(spec)


@pytest.mark.parametrize(
    ("quality", "orientation", "expected"),
    [
        ("hd_1080p", "horizontal", (1920, 1080)),
        ("hd_1080p", "vertical", (1080, 1920)),
        ("hd_1080p", "square", (1080, 1080)),
        ("sd", "horizontal", (854, 480)),
        ("uhd_4k", "vertical", (2160, 3840)),
    ],
)
def test_target_box_orients_the_ladder(
    quality: str, orientation: str, expected: tuple[int, int]
) -> None:
    assert _Exporter._target_box(quality, orientation) == expected


async def _probe_video_stream(path: Path) -> dict[str, int]:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "quiet",
        "-select_streams",
        "v:0",
        "-print_format",
        "json",
        "-show_streams",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    data = json.loads(stdout.decode())
    return data["streams"][0]


async def _make_master(path: Path, *, size: str = "640x360") -> None:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=duration=1:size={size}:rate=15",
        "-pix_fmt",
        "yuv420p",
        str(path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.communicate()
    assert proc.returncode == 0


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")
async def test_real_ffmpeg_exports_mp4_preserving_orientation(tmp_path: Path) -> None:
    # A 640x360 (horizontal) master exported to sd horizontal → fits in the 854x480 box,
    # preserves aspect (no pad), stays horizontal (Fork F).
    src = tmp_path / "master.mp4"
    await _make_master(src, size="640x360")
    exporter = FfmpegExporter(timeout_seconds=120.0)
    out = tmp_path / "out.mp4"

    result = await exporter.export(
        ExportSpec(
            source_path=str(src),
            output_path=str(out),
            format="mp4",
            quality="sd",
            orientation="horizontal",
        )
    )

    assert out.exists()
    assert result.size_bytes > 0
    assert result.mime_type == "video/mp4"
    assert result.width is not None and result.height is not None
    assert result.width >= result.height  # still horizontal
    assert result.width <= 854 and result.height <= 480  # within the sd box


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")
async def test_real_ffmpeg_exports_gif(tmp_path: Path) -> None:
    src = tmp_path / "master.mp4"
    await _make_master(src, size="640x360")
    exporter = FfmpegExporter(timeout_seconds=120.0)
    out = tmp_path / "out.gif"

    result = await exporter.export(
        ExportSpec(
            source_path=str(src),
            output_path=str(out),
            format="gif",
            quality="sd",
            orientation="horizontal",
        )
    )

    assert out.exists()
    assert result.size_bytes > 0
    assert result.mime_type == "image/gif"
    stream = await _probe_video_stream(out)
    assert stream["codec_name"] == "gif"
