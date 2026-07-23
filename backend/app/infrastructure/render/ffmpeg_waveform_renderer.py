"""FFmpeg ``IWaveformRenderer`` adapter (Slice α8.4d).

Renders a fixed-size waveform PNG from the source's audio track via the
``showwavespic`` filter. Sources with **no audio stream** return ``None`` (not
applicable — not a failure), so a silent video is still terminally enriched.
Configuration-blind (W8.1.1). Genuine engine failures map to ``WaveformError``.
Exercised by an **opt-in** integration test.
"""

from __future__ import annotations

import asyncio
import os
import tempfile

from app.application.interfaces.waveform_renderer import (
    IWaveformRenderer,
    Waveform,
    WaveformError,
)
from app.infrastructure.render._ffmpeg_exec import probe_has_audio, run_command

_MIME = "image/png"


class FfmpegWaveformRenderer(IWaveformRenderer):
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

    async def waveform(self, *, source_path: str, width: int, height: int) -> Waveform | None:
        if width <= 0 or height <= 0:
            raise WaveformError(f"width/height must be > 0 (got {width}x{height})")

        has_audio = await probe_has_audio(
            ffprobe_path=self._ffprobe,
            source_path=source_path,
            timeout=self._timeout,
            error_cls=WaveformError,
        )
        if not has_audio:
            return None  # not applicable — silent source

        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "waveform.png")
            args = [
                self._ffmpeg,
                "-y",
                "-i",
                source_path,
                "-filter_complex",
                f"showwavespic=s={width}x{height}",
                "-frames:v",
                "1",
                out,
            ]
            await run_command(
                args, timeout=self._timeout, error_cls=WaveformError, what="ffmpeg waveform"
            )
            if not os.path.isfile(out) or os.path.getsize(out) == 0:
                raise WaveformError("ffmpeg reported success but produced no waveform")
            data = await asyncio.to_thread(_read_bytes, out)

        return Waveform(data=data, mime_type=_MIME, width=width, height=height)


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()
