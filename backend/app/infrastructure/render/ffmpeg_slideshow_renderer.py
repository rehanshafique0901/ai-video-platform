"""FFmpeg slideshow renderer (implements ``ISlideshowRenderer``).

The canonical renderer for the generation slice: an ordered list of image frames
with per-frame durations becomes a single H.264/mp4. It supports per-image
duration, an optional simple crossfade, an optional audio track, and a target
resolution (e.g. 720p/1080p). It is provider-agnostic about where the frames came
from and returns bytes, so the use case never deals with temp paths.

Output is made as deterministic as ffmpeg allows (fixed fps, yuv420p, bitexact,
fixed preset, no timestamps embedded). Heavier transitions/effects are out of
scope by design — the point is a reliable base renderer.
"""

from __future__ import annotations

import os
import tempfile

from app.application.interfaces.slideshow_renderer import (
    ISlideshowRenderer,
    RenderedVideo,
    SlideshowFrame,
    SlideshowRenderError,
    SlideshowSpec,
)
from app.infrastructure.render._ffmpeg_exec import run_command


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def _write_bytes(path: str, data: bytes) -> None:
    with open(path, "wb") as fh:
        fh.write(data)


def _scale_filter(width: int, height: int, fps: int) -> str:
    """Fit-inside + pad to exactly WxH, square pixels, fixed fps, yuv420p."""
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"setsar=1,fps={fps},format=yuv420p"
    )


def _build_filter_complex(
    durations: list[float], *, width: int, height: int, fps: int, crossfade: float
) -> tuple[str, str]:
    """Return (filter_complex, final_video_label)."""
    n = len(durations)
    scale = _scale_filter(width, height, fps)
    steps = [f"[{i}:v]{scale}[v{i}]" for i in range(n)]

    if n == 1:
        return ";".join(steps), "[v0]"

    use_xfade = crossfade > 0.0 and crossfade < min(durations)
    if not use_xfade:
        concat_inputs = "".join(f"[v{i}]" for i in range(n))
        steps.append(f"{concat_inputs}concat=n={n}:v=1:a=0[vout]")
        return ";".join(steps), "[vout]"

    # Crossfade chain: each xfade starts `crossfade` before the running end.
    prev = "[v0]"
    acc = durations[0]
    for i in range(1, n):
        offset = acc - crossfade
        out = f"[x{i}]" if i < n - 1 else "[vout]"
        steps.append(
            f"{prev}[v{i}]xfade=transition=fade:duration={crossfade}:" f"offset={offset:.3f}{out}"
        )
        prev = out
        acc = acc + durations[i] - crossfade
    return ";".join(steps), "[vout]"


class FfmpegSlideshowRenderer(ISlideshowRenderer):
    def __init__(
        self,
        *,
        ffmpeg_path: str = "ffmpeg",
        timeout_seconds: float = 900.0,
    ) -> None:
        self._ffmpeg = ffmpeg_path
        self._timeout = timeout_seconds

    async def render(
        self,
        *,
        frames: tuple[SlideshowFrame, ...],
        spec: SlideshowSpec,
        audio: bytes | None = None,
    ) -> RenderedVideo:
        if not frames:
            raise SlideshowRenderError("cannot render a slideshow with no frames")
        if any(f.duration_seconds <= 0 for f in frames):
            raise SlideshowRenderError("every frame must have a positive duration")
        if spec.width <= 0 or spec.height <= 0 or spec.fps <= 0:
            raise SlideshowRenderError("width, height and fps must be positive")

        return await self._render_async(frames, spec, audio)

    async def _render_async(
        self, frames: tuple[SlideshowFrame, ...], spec: SlideshowSpec, audio: bytes | None
    ) -> RenderedVideo:
        with tempfile.TemporaryDirectory(prefix="slideshow_") as tmp:
            args: list[str] = [self._ffmpeg, "-y", "-nostdin"]
            durations = [f.duration_seconds for f in frames]
            for i, frame in enumerate(frames):
                path = os.path.join(tmp, f"frame_{i:04d}")
                _write_bytes(path, frame.data)
                args += ["-loop", "1", "-t", f"{frame.duration_seconds}", "-i", path]

            audio_index = len(frames)
            if audio is not None:
                audio_path = os.path.join(tmp, "audio_track")
                _write_bytes(audio_path, audio)
                args += ["-i", audio_path]

            filter_complex, vlabel = _build_filter_complex(
                durations,
                width=spec.width,
                height=spec.height,
                fps=spec.fps,
                crossfade=spec.crossfade_seconds,
            )
            out_path = os.path.join(tmp, f"out.{spec.container}")
            args += [
                "-filter_complex",
                filter_complex,
                "-map",
                vlabel,
                "-r",
                str(spec.fps),
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-pix_fmt",
                "yuv420p",
                "-fflags",
                "+bitexact",
                "-flags:v",
                "+bitexact",
            ]
            if audio is not None:
                args += ["-map", f"{audio_index}:a", "-c:a", "aac", "-shortest"]
            args += ["-movflags", "+faststart", out_path]

            await run_command(
                args,
                timeout=self._timeout,
                error_cls=SlideshowRenderError,
                what="ffmpeg slideshow",
            )
            return RenderedVideo(data=_read_bytes(out_path), content_type="video/mp4")
