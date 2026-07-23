"""FFmpeg ``IGifPreviewer`` adapter (Slice α8.4d).

Samples the first ``max_seconds`` of the source at ``fps`` and downscales to
``max_width`` (lanczos), producing an animated GIF. Configuration-blind (W8.1.1). Any
non-zero exit / timeout / missing output maps to a neutral ``GifPreviewError``.
Exercised by an **opt-in** integration test.
"""

from __future__ import annotations

import asyncio
import os
import tempfile

from app.application.interfaces.gif_previewer import (
    GifPreview,
    GifPreviewError,
    IGifPreviewer,
)
from app.infrastructure.render._ffmpeg_exec import probe_dimensions, run_command

_MIME = "image/gif"


class FfmpegGifPreviewer(IGifPreviewer):
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

    async def gif(
        self, *, source_path: str, max_seconds: float, fps: int, max_width: int
    ) -> GifPreview:
        if max_seconds <= 0:
            raise GifPreviewError(f"max_seconds must be > 0 (got {max_seconds})")
        if fps <= 0:
            raise GifPreviewError(f"fps must be > 0 (got {fps})")
        if max_width <= 0:
            raise GifPreviewError(f"max_width must be > 0 (got {max_width})")

        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "preview.gif")
            args = [
                self._ffmpeg,
                "-y",
                "-i",
                source_path,
                "-t",
                str(max_seconds),
                "-vf",
                f"fps={fps},scale='min({max_width},iw)':-1:flags=lanczos",
                out,
            ]
            await run_command(
                args, timeout=self._timeout, error_cls=GifPreviewError, what="ffmpeg gif"
            )
            if not os.path.isfile(out) or os.path.getsize(out) == 0:
                raise GifPreviewError("ffmpeg reported success but produced no gif")
            data = await asyncio.to_thread(_read_bytes, out)
            width, height = await probe_dimensions(
                ffprobe_path=self._ffprobe,
                source_path=out,
                timeout=self._timeout,
                error_cls=GifPreviewError,
            )

        return GifPreview(data=data, mime_type=_MIME, width=width, height=height)


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()
