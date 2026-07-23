"""FFmpeg ``IRenderer`` adapter (Slice α8.4b).

Composes ordered, trimmed source clips into one output video by shelling out to the
``ffmpeg`` binary (``filter_complex`` trim + concat), then probes the result with
``ffprobe``. Configuration-blind (W8.1.1): binary paths + a timeout are injected;
nothing here reads env/DB/secrets. Any non-zero exit, timeout, missing output, or
probe failure maps to a neutral ``RenderError`` — no subprocess detail leaks up.

α8.4b baseline scope (Fork E): video concat with per-clip trims. Audio mixing,
transitions/effects, thumbnails, and previews are α8.4c / α6.4. Because the real
binary is required, this adapter is exercised by an **opt-in integration test**
(skipped when ``ffmpeg`` is unavailable); use-case unit tests use a fake renderer.
"""

from __future__ import annotations

import asyncio
import json
import os

from app.application.interfaces.renderer import (
    IRenderer,
    RenderError,
    RenderResult,
    RenderSpec,
)


class FfmpegRenderer(IRenderer):
    """Render via the local ``ffmpeg``/``ffprobe`` binaries."""

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

    async def render(self, spec: RenderSpec) -> RenderResult:
        if not spec.inputs:
            raise RenderError("render spec has no inputs")

        args = [self._ffmpeg, "-y"]
        for clip in spec.inputs:
            args += ["-i", clip.path]

        # filter_complex: trim each input's video to its source window, reset PTS,
        # then concat in order. Video-only baseline (a=0) for α8.4b.
        parts: list[str] = []
        labels: list[str] = []
        for i, clip in enumerate(spec.inputs):
            if clip.source_end_seconds <= clip.source_start_seconds:
                raise RenderError(
                    f"input {i} has non-positive trim window "
                    f"({clip.source_start_seconds}..{clip.source_end_seconds})"
                )
            parts.append(
                f"[{i}:v]trim=start={clip.source_start_seconds}:end={clip.source_end_seconds},"
                f"setpts=PTS-STARTPTS[v{i}]"
            )
            labels.append(f"[v{i}]")
        filtergraph = (
            ";".join(parts) + ";" + "".join(labels) + f"concat=n={len(spec.inputs)}:v=1:a=0[outv]"
        )

        args += [
            "-filter_complex",
            filtergraph,
            "-map",
            "[outv]",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            spec.output_path,
        ]

        await self._run(args, what="ffmpeg render")

        if not os.path.isfile(spec.output_path):
            raise RenderError("ffmpeg reported success but produced no output file")

        size_bytes = os.path.getsize(spec.output_path)
        duration, width, height, codec = await self._probe(spec.output_path)
        return RenderResult(
            output_path=spec.output_path,
            size_bytes=size_bytes,
            duration_seconds=duration,
            width=width,
            height=height,
            codec=codec,
        )

    async def _run(self, args: list[str], *, what: str) -> tuple[bytes, bytes]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, ValueError) as exc:
            raise RenderError(f"failed to launch {what}: {exc}") from exc
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
        except TimeoutError as exc:
            proc.kill()
            raise RenderError(f"{what} timed out after {self._timeout}s") from exc
        if proc.returncode != 0:
            tail = stderr.decode("utf-8", "replace")[-500:]
            raise RenderError(f"{what} exited {proc.returncode}: {tail}")
        return stdout, stderr

    async def _probe(self, path: str) -> tuple[float | None, int | None, int | None, str | None]:
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
            return None, None, None, None

        duration: float | None = None
        fmt = data.get("format")
        if isinstance(fmt, dict):
            raw = fmt.get("duration")
            try:
                duration = float(raw) if raw is not None else None
            except (TypeError, ValueError):
                duration = None

        width = height = None
        codec = None
        for stream in data.get("streams", []) or []:
            if isinstance(stream, dict) and stream.get("codec_type") == "video":
                w, h = stream.get("width"), stream.get("height")
                width = int(w) if isinstance(w, int) else None
                height = int(h) if isinstance(h, int) else None
                c = stream.get("codec_name")
                codec = str(c) if isinstance(c, str) else None
                break
        return duration, width, height, codec
