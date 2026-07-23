"""FFmpeg ``IPreviewClipper`` adapter (Slice α8.4d).

Trims the source to a max duration and downscales to a max width (never upscales),
re-encoding to a web-friendly MP4. Configuration-blind (W8.1.1): binary paths +
timeout injected. Any non-zero exit / timeout / missing output maps to a neutral
``PreviewClipError``. Exercised by an **opt-in** integration test.
"""

from __future__ import annotations

import asyncio
import os
import tempfile

from app.application.interfaces.preview_clipper import (
    IPreviewClipper,
    PreviewClip,
    PreviewClipError,
)
from app.infrastructure.render._ffmpeg_exec import (
    probe_dimensions,
    probe_duration,
    run_command,
)

_MIME = "video/mp4"


class FfmpegPreviewClipper(IPreviewClipper):
    def __init__(
        self,
        *,
        ffmpeg_path: str = "ffmpeg",
        ffprobe_path: str = "ffprobe",
        timeout_seconds: float = 120.0,
    ) -> None:
        self._ffmpeg = ffmpeg_path
        self._ffprobe = ffprobe_path
        self._timeout = timeout_seconds

    async def preview(self, *, source_path: str, max_seconds: float, max_width: int) -> PreviewClip:
        if max_seconds <= 0:
            raise PreviewClipError(f"max_seconds must be > 0 (got {max_seconds})")
        if max_width <= 0:
            raise PreviewClipError(f"max_width must be > 0 (got {max_width})")

        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "preview.mp4")
            # Downscale only if wider than max_width; keep even height (-2). Drop audio
            # (a preview is visual); web-friendly faststart + yuv420p.
            args = [
                self._ffmpeg,
                "-y",
                "-i",
                source_path,
                "-t",
                str(max_seconds),
                "-an",
                "-vf",
                f"scale='min({max_width},iw)':-2",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                out,
            ]
            await run_command(
                args, timeout=self._timeout, error_cls=PreviewClipError, what="ffmpeg preview"
            )
            if not os.path.isfile(out) or os.path.getsize(out) == 0:
                raise PreviewClipError("ffmpeg reported success but produced no preview")
            data = await asyncio.to_thread(_read_bytes, out)
            width, height = await probe_dimensions(
                ffprobe_path=self._ffprobe,
                source_path=out,
                timeout=self._timeout,
                error_cls=PreviewClipError,
            )
            duration = await probe_duration(
                ffprobe_path=self._ffprobe,
                source_path=out,
                timeout=self._timeout,
                error_cls=PreviewClipError,
            )

        return PreviewClip(
            data=data,
            mime_type=_MIME,
            width=width,
            height=height,
            duration_seconds=duration,
        )


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()
