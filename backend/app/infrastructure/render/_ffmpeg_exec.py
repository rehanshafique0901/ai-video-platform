"""Shared subprocess helpers for the α8.4d derived-preview FFmpeg adapters.

Small internal utilities so `FfmpegPreviewClipper` / `FfmpegGifPreviewer` /
`FfmpegWaveformRenderer` don't each re-implement launch/timeout/exit handling. Every
failure is mapped to the caller-supplied neutral error type — no subprocess detail
leaks past the port boundary. Kept private to the render infrastructure package.
"""

from __future__ import annotations

import asyncio
import json


async def run_command(
    args: list[str], *, timeout: float, error_cls: type[Exception], what: str
) -> bytes:
    """Run ``args`` to completion; return stdout. Map every failure to ``error_cls``."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, ValueError) as exc:
        raise error_cls(f"failed to launch {what}: {exc}") from exc
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError as exc:
        proc.kill()
        raise error_cls(f"{what} timed out after {timeout}s") from exc
    if proc.returncode != 0:
        tail = stderr.decode("utf-8", "replace")[-500:]
        raise error_cls(f"{what} exited {proc.returncode}: {tail}")
    return stdout


async def probe_dimensions(
    *, ffprobe_path: str, source_path: str, timeout: float, error_cls: type[Exception]
) -> tuple[int | None, int | None]:
    """Best-effort width/height of the first video stream (``None`` on any parse miss)."""
    args = [
        ffprobe_path,
        "-v",
        "quiet",
        "-select_streams",
        "v:0",
        "-print_format",
        "json",
        "-show_streams",
        source_path,
    ]
    stdout = await run_command(
        args, timeout=timeout, error_cls=error_cls, what="ffprobe dimensions"
    )
    try:
        data = json.loads(stdout.decode("utf-8", "replace"))
        stream = (data.get("streams") or [{}])[0]
        w, h = stream.get("width"), stream.get("height")
        return (int(w) if isinstance(w, int) else None, int(h) if isinstance(h, int) else None)
    except (ValueError, UnicodeError, IndexError):
        return None, None


async def probe_duration(
    *, ffprobe_path: str, source_path: str, timeout: float, error_cls: type[Exception]
) -> float | None:
    """Best-effort container duration in seconds (``None`` on any parse miss)."""
    args = [
        ffprobe_path,
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        source_path,
    ]
    stdout = await run_command(args, timeout=timeout, error_cls=error_cls, what="ffprobe duration")
    try:
        data = json.loads(stdout.decode("utf-8", "replace"))
        raw = (data.get("format") or {}).get("duration")
        return float(raw) if raw is not None else None
    except (ValueError, UnicodeError, TypeError):
        return None


async def probe_has_audio(
    *, ffprobe_path: str, source_path: str, timeout: float, error_cls: type[Exception]
) -> bool:
    """True iff the source declares at least one audio stream."""
    args = [
        ffprobe_path,
        "-v",
        "quiet",
        "-select_streams",
        "a",
        "-show_entries",
        "stream=index",
        "-print_format",
        "json",
        source_path,
    ]
    stdout = await run_command(args, timeout=timeout, error_cls=error_cls, what="ffprobe audio")
    try:
        data = json.loads(stdout.decode("utf-8", "replace"))
        return bool(data.get("streams"))
    except (ValueError, UnicodeError):
        return False
