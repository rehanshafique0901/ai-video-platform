"""Render engine adapters (Slice α8.4b).

α8.4b ships :class:`FfmpegRenderer` behind the neutral ``IRenderer`` port — it
shells out to the ``ffmpeg``/``ffprobe`` binaries to compose a Timeline's resolved
clips into a single output video. A different engine could replace it with no
use-case change.
"""

from __future__ import annotations

from app.infrastructure.render.ffmpeg_renderer import FfmpegRenderer
from app.infrastructure.render.ffmpeg_thumbnailer import FfmpegThumbnailer

__all__ = ["FfmpegRenderer", "FfmpegThumbnailer"]
