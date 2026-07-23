"""FFmpeg ``IThumbnailer`` adapter (Slice α8.4c).

Extracts one still frame at a timestamp (`ffmpeg -ss … -frames:v 1`) and probes the
source's bitrate (`ffprobe`). Configuration-blind (W8.1.1): binary paths + a timeout
are injected. Any non-zero exit, timeout, or missing output maps to a neutral
``ThumbnailError`` — no subprocess detail leaks up. Shares the α8.4b FFmpeg config.

Because the real binary is required, this adapter is exercised by an **opt-in
integration test** (skipped when `ffmpeg` is unavailable); use-case unit tests use a
fake thumbnailer.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile

from app.application.interfaces.thumbnailer import IThumbnailer, Thumbnail, ThumbnailError

_THUMB_MIME = "image/jpeg"


class FfmpegThumbnailer(IThumbnailer):
    """Derive thumbnails via the local ``ffmpeg``/``ffprobe`` binaries."""

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

    async def thumbnail(self, *, source_path: str, at_seconds: float) -> Thumbnail:
        if at_seconds < 0:
            raise ThumbnailError(f"at_seconds must be >= 0 (got {at_seconds})")

        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "thumb.jpg")
            # -ss before -i = fast input seek; one frame; overwrite.
            args = [
                self._ffmpeg,
                "-y",
                "-ss",
                str(at_seconds),
                "-i",
                source_path,
                "-frames:v",
                "1",
                "-q:v",
                "2",
                out,
            ]
            await self._run(args, what="ffmpeg thumbnail")
            if not os.path.isfile(out) or os.path.getsize(out) == 0:
                raise ThumbnailError("ffmpeg reported success but produced no thumbnail")
            image = await asyncio.to_thread(_read_bytes, out)

        width, height = await self._probe_dimensions(source_path)
        bitrate = await self._probe_bitrate(source_path)
        return Thumbnail(
            image=image,
            mime_type=_THUMB_MIME,
            width=width,
            height=height,
            source_bitrate=bitrate,
        )

    async def _run(self, args: list[str], *, what: str) -> bytes:
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, ValueError) as exc:
            raise ThumbnailError(f"failed to launch {what}: {exc}") from exc
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
        except TimeoutError as exc:
            proc.kill()
            raise ThumbnailError(f"{what} timed out after {self._timeout}s") from exc
        if proc.returncode != 0:
            tail = stderr.decode("utf-8", "replace")[-500:]
            raise ThumbnailError(f"{what} exited {proc.returncode}: {tail}")
        return stdout

    async def _probe_dimensions(self, path: str) -> tuple[int | None, int | None]:
        args = [
            self._ffprobe,
            "-v",
            "quiet",
            "-select_streams",
            "v:0",
            "-print_format",
            "json",
            "-show_streams",
            path,
        ]
        stdout = await self._run(args, what="ffprobe dimensions")
        try:
            data = json.loads(stdout.decode("utf-8", "replace"))
            stream = (data.get("streams") or [{}])[0]
            w, h = stream.get("width"), stream.get("height")
            return (int(w) if isinstance(w, int) else None, int(h) if isinstance(h, int) else None)
        except (ValueError, UnicodeError, IndexError):
            return None, None

    async def _probe_bitrate(self, path: str) -> int | None:
        args = [
            self._ffprobe,
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            path,
        ]
        stdout = await self._run(args, what="ffprobe bitrate")
        try:
            data = json.loads(stdout.decode("utf-8", "replace"))
            raw = (data.get("format") or {}).get("bit_rate")
            return int(raw) if raw is not None else None
        except (ValueError, UnicodeError, TypeError):
            return None


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()
