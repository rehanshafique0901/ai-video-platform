"""FFmpeg ``IExporter`` adapter (Slice α8.5a).

Transcodes a finished master render into one delivery encoding by shelling out to the
``ffmpeg`` binary, then probes the result with ``ffprobe``. Configuration-blind (W8.1.1):
binary paths + a timeout are injected; nothing here reads env / DB / secrets. Any non-zero
exit, timeout, missing output, or unknown parameter maps to a neutral ``ExportError`` — no
subprocess detail leaks up. Self-contained (own ``_run``/``_probe``) so export stays a
distinct domain from render (α8.5a Fork C), mirroring ``FfmpegRenderer``.

**Delivery-only, same presentation (Fork F, tightened).** ``quality`` selects a fixed
resolution **box**; ``orientation`` orients that box (the caller guarantees it matches the
source, so this is never a reframe); ``format`` selects the container/codec. Scaling
preserves the source aspect ratio and **never pads/crops** (``force_original_aspect_ratio=
decrease`` + even-dimension rounding) — presentation is untouched, only delivery
characteristics change. Every parameter is a pure function of the spec (RC6 / RP9). Audio
(produced by α8.4e) is carried through for video containers; ``gif`` drops audio.

Because the real binary is required, this adapter is exercised by an **opt-in integration
test** (skipped when ``ffmpeg`` is unavailable); use-case unit tests use a fake exporter.
"""

from __future__ import annotations

import asyncio
import json
import os

from app.application.interfaces.exporter import (
    EXPORT_FORMAT_MIME,
    ExportError,
    ExportResult,
    ExportSpec,
    IExporter,
)

# Resolution ladder: quality → (long_edge, short_edge). ``orientation`` orients the box;
# scaling fits the source inside it preserving aspect (no pad), so these are upper bounds.
_LADDER: dict[str, tuple[int, int]] = {
    "sd": (854, 480),
    "hd_1080p": (1920, 1080),
    "qhd_2k": (2560, 1440),
    "uhd_4k": (3840, 2160),
}

_ORIENTATIONS = frozenset({"horizontal", "vertical", "square"})

# Fixed, deterministic encode knobs (RP9). x264/vp9 constant-quality; gif frame rate.
_X264_CRF = "23"
_VP9_CRF = "32"
_GIF_FPS = 15


class FfmpegExporter(IExporter):
    """Export via the local ``ffmpeg``/``ffprobe`` binaries (delivery-only transcode)."""

    def __init__(
        self,
        *,
        ffmpeg_path: str = "ffmpeg",
        ffprobe_path: str = "ffprobe",
        timeout_seconds: float = 900.0,
    ) -> None:
        self._ffmpeg = ffmpeg_path
        self._ffprobe = ffprobe_path
        self._timeout = timeout_seconds

    async def export(self, spec: ExportSpec) -> ExportResult:
        if spec.format not in EXPORT_FORMAT_MIME:
            raise ExportError(f"unsupported export format {spec.format!r}")
        if spec.quality not in _LADDER:
            raise ExportError(f"unsupported export quality {spec.quality!r}")
        if spec.orientation not in _ORIENTATIONS:
            raise ExportError(f"unsupported export orientation {spec.orientation!r}")
        if not os.path.isfile(spec.source_path):
            raise ExportError("export source file does not exist")

        target_w, target_h = self._target_box(spec.quality, spec.orientation)

        if spec.format == "gif":
            args = self._gif_args(spec, target_w, target_h)
        else:
            has_audio = await self._probe_has_audio(spec.source_path)
            args = self._video_args(spec, target_w, target_h, has_audio=has_audio)

        await self._run(args, what="ffmpeg export")

        if not os.path.isfile(spec.output_path):
            raise ExportError("ffmpeg reported success but produced no output file")

        size_bytes = os.path.getsize(spec.output_path)
        duration, width, height = await self._probe(spec.output_path)
        return ExportResult(
            output_path=spec.output_path,
            size_bytes=size_bytes,
            mime_type=EXPORT_FORMAT_MIME[spec.format],
            duration_seconds=duration,
            width=width,
            height=height,
        )

    # ---- filter graph / args -------------------------------------------

    @staticmethod
    def _target_box(quality: str, orientation: str) -> tuple[int, int]:
        long_edge, short_edge = _LADDER[quality]
        if orientation == "horizontal":
            return long_edge, short_edge
        if orientation == "vertical":
            return short_edge, long_edge
        return short_edge, short_edge  # square

    @staticmethod
    def _scale_vf(target_w: int, target_h: int) -> str:
        # Fit within the box preserving aspect (no pad/crop → presentation preserved),
        # then round to even dimensions (required by yuv420p / libx264).
        return (
            f"scale=w={target_w}:h={target_h}:force_original_aspect_ratio=decrease,"
            f"scale=trunc(iw/2)*2:trunc(ih/2)*2"
        )

    def _video_args(
        self, spec: ExportSpec, target_w: int, target_h: int, *, has_audio: bool
    ) -> list[str]:
        args = [
            self._ffmpeg,
            "-y",
            "-i",
            spec.source_path,
            "-vf",
            self._scale_vf(target_w, target_h),
        ]
        if spec.format == "webm":
            args += ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", _VP9_CRF]
            args += ["-c:a", "libopus"] if has_audio else ["-an"]
        else:  # mp4 / mov → h264 + aac
            args += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", _X264_CRF]
            args += ["-c:a", "aac"] if has_audio else ["-an"]
            if spec.format == "mp4":
                args += ["-movflags", "+faststart"]
        args += [spec.output_path]
        return args

    def _gif_args(self, spec: ExportSpec, target_w: int, target_h: int) -> list[str]:
        # Single-graph palettegen/paletteuse for a deterministic, high-quality GIF; no audio.
        graph = (
            f"fps={_GIF_FPS},{self._scale_vf(target_w, target_h)},"
            f"split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse"
        )
        return [
            self._ffmpeg,
            "-y",
            "-i",
            spec.source_path,
            "-filter_complex",
            graph,
            "-an",
            spec.output_path,
        ]

    # ---- subprocess plumbing -------------------------------------------

    async def _probe_has_audio(self, path: str) -> bool:
        args = [
            self._ffprobe,
            "-v",
            "quiet",
            "-select_streams",
            "a",
            "-show_entries",
            "stream=index",
            "-of",
            "csv=p=0",
            path,
        ]
        stdout, _ = await self._run(args, what="ffprobe audio")
        return bool(stdout.decode("utf-8", "replace").strip())

    async def _run(self, args: list[str], *, what: str) -> tuple[bytes, bytes]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, ValueError) as exc:
            raise ExportError(f"failed to launch {what}: {exc}") from exc
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
        except TimeoutError as exc:
            proc.kill()
            raise ExportError(f"{what} timed out after {self._timeout}s") from exc
        if proc.returncode != 0:
            tail = stderr.decode("utf-8", "replace")[-500:]
            raise ExportError(f"{what} exited {proc.returncode}: {tail}")
        return stdout, stderr

    async def _probe(self, path: str) -> tuple[float | None, int | None, int | None]:
        args = [
            self._ffprobe,
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            path,
        ]
        stdout, _ = await self._run(args, what="ffprobe")
        try:
            data = json.loads(stdout.decode("utf-8", "replace"))
        except (ValueError, UnicodeError):
            return None, None, None

        duration: float | None = None
        fmt = data.get("format")
        if isinstance(fmt, dict):
            raw = fmt.get("duration")
            try:
                duration = float(raw) if raw is not None else None
            except (TypeError, ValueError):
                duration = None

        width = height = None
        for stream in data.get("streams", []) or []:
            if isinstance(stream, dict) and stream.get("codec_type") == "video":
                w, h = stream.get("width"), stream.get("height")
                width = int(w) if isinstance(w, int) else None
                height = int(h) if isinstance(h, int) else None
                break
        return duration, width, height
