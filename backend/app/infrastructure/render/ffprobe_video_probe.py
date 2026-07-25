"""ffprobe video probe (implements ``IVideoProbe``).

Measures a rendered video (duration + dimensions) so the use case's video
verifier can confirm the render matches the plan. Accepts bytes, writes them to a
temp file, and reuses the shared ffprobe helpers. Best-effort: unparseable values
come back as ``None`` (reported as "not measured", never a silent pass).
"""

from __future__ import annotations

import os
import tempfile

from app.application.interfaces.video_probe import IVideoProbe, ObservedVideo
from app.infrastructure.render._ffmpeg_exec import probe_dimensions, probe_duration


class ProbeError(RuntimeError):
    """Raised when ffprobe cannot be launched/executed on the video."""


class FfprobeVideoProbe(IVideoProbe):
    def __init__(self, *, ffprobe_path: str = "ffprobe", timeout_seconds: float = 120.0) -> None:
        self._ffprobe = ffprobe_path
        self._timeout = timeout_seconds

    async def probe(self, video: bytes) -> ObservedVideo:
        with tempfile.TemporaryDirectory(prefix="probe_") as tmp:
            path = os.path.join(tmp, "video.mp4")
            with open(path, "wb") as fh:
                fh.write(video)
            width, height = await self._safe_dimensions(path)
            duration = await self._safe_duration(path)
        return ObservedVideo(duration_seconds=duration, width=width, height=height)

    async def _safe_dimensions(self, path: str) -> tuple[int | None, int | None]:
        # Best-effort: an unprobeable (e.g. corrupt) file yields "not measured"
        # rather than crashing the pipeline. A valid render always probes cleanly.
        try:
            return await probe_dimensions(
                ffprobe_path=self._ffprobe,
                source_path=path,
                timeout=self._timeout,
                error_cls=ProbeError,
            )
        except ProbeError:
            return None, None

    async def _safe_duration(self, path: str) -> float | None:
        try:
            return await probe_duration(
                ffprobe_path=self._ffprobe,
                source_path=path,
                timeout=self._timeout,
                error_cls=ProbeError,
            )
        except ProbeError:
            return None
