"""FFmpeg ``IRenderer`` adapter (Slice α8.4b + α8.4e audio mixing).

Composes ordered, trimmed source clips into one output video by shelling out to the
``ffmpeg`` binary (``filter_complex`` trim + concat), then probes the result with
``ffprobe``. Configuration-blind (W8.1.1): binary paths + a timeout are injected;
nothing here reads env/DB/secrets. Any non-zero exit, timeout, missing output, or
probe failure maps to a neutral ``RenderError`` — no subprocess detail leaks up.

α8.4b baseline: video concat with per-clip trims (``a=0``, audio discarded).
α8.4e adds **deterministic audio mixing**:

* each video clip's own audio travels with its segment (a synced *bed*), included at
  ``RenderInput.volume`` gain unless ``muted`` — a clip that contributes no audio is
  silence-filled for its duration so the bed stays synced to the concatenated video;
* dedicated ``AudioInput`` tracks (music / voiceover) are trimmed, gained, delayed to
  their ``start_seconds`` (``adelay``), and mixed over the bed with ``amix``;
* the mix is a **pure weighted sum** — ``amix=…:normalize=0`` (no implicit gain
  staging), no ducking / compression / normalization / fades (invariant **W8.4e.1**);
* a timeline with **no** authored audio anywhere renders a silent video (``a=0``),
  exactly as α8.4b.

Transitions/effects/color-grading/subtitle burn-in are α8.4f (they require α6.4
Timeline authoring). Because the real binary is required, this adapter is exercised by
an **opt-in integration test** (skipped when ``ffmpeg`` is unavailable); use-case unit
tests use a fake renderer.
"""

from __future__ import annotations

import asyncio
import json
import os

from app.application.interfaces.renderer import (
    AudioInput,
    IRenderer,
    RenderError,
    RenderResult,
    RenderSpec,
)

# Common audio format for every branch of the mix so ``concat``/``amix`` see matching
# streams (stereo, 44.1 kHz, planar float). Fixed constants keep the mix deterministic.
_A_SR = 44100
_A_LAYOUT = "stereo"
_AFMT = f"aformat=sample_fmts=fltp:sample_rates={_A_SR}:channel_layouts={_A_LAYOUT}"


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

        # Validate trim windows up front (video + audio) — deterministic failure.
        for i, clip in enumerate(spec.inputs):
            if clip.source_end_seconds <= clip.source_start_seconds:
                raise RenderError(
                    f"input {i} has non-positive trim window "
                    f"({clip.source_start_seconds}..{clip.source_end_seconds})"
                )
        for k, audio in enumerate(spec.audio_inputs):
            if audio.source_end_seconds <= audio.source_start_seconds:
                raise RenderError(
                    f"audio input {k} has non-positive trim window "
                    f"({audio.source_start_seconds}..{audio.source_end_seconds})"
                )
            if audio.start_seconds < 0:
                raise RenderError(f"audio input {k} has negative start ({audio.start_seconds})")

        # Decide which sources actually contribute audio (probe only when it could).
        clip_has_audio: list[bool] = []
        for clip in spec.inputs:
            contributes = (not clip.muted) and clip.volume > 0
            if contributes:
                contributes = await self._probe_has_audio(clip.path)
            clip_has_audio.append(contributes)
        included_audio = [
            audio
            for audio in spec.audio_inputs
            if audio.volume > 0 and await self._probe_has_audio(audio.path)
        ]
        has_any_audio = any(clip_has_audio) or bool(included_audio)

        # ffmpeg inputs: video clips (0..N-1), then the contributing audio tracks.
        n = len(spec.inputs)
        args = [self._ffmpeg, "-y"]
        for clip in spec.inputs:
            args += ["-i", clip.path]
        if has_any_audio:
            for audio in included_audio:
                args += ["-i", audio.path]

        # Video: trim each input, reset PTS, concat in order (α8.4b, unchanged).
        parts: list[str] = []
        vlabels: list[str] = []
        for i, clip in enumerate(spec.inputs):
            parts.append(
                f"[{i}:v]trim=start={clip.source_start_seconds}:end={clip.source_end_seconds},"
                f"setpts=PTS-STARTPTS[v{i}]"
            )
            vlabels.append(f"[v{i}]")
        parts.append("".join(vlabels) + f"concat=n={n}:v=1:a=0[outv]")

        maps = ["-map", "[outv]"]
        if has_any_audio:
            out_audio = self._build_audio_graph(spec, clip_has_audio, included_audio, n, parts)
            maps += ["-map", out_audio]

        args += [
            "-filter_complex",
            ";".join(parts),
            *maps,
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
        ]
        if has_any_audio:
            args += ["-c:a", "aac"]
        args += [spec.output_path]

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

    def _build_audio_graph(
        self,
        spec: RenderSpec,
        clip_has_audio: list[bool],
        included_audio: list[AudioInput],
        n: int,
        parts: list[str],
    ) -> str:
        """Append the audio filter graph to ``parts``; return the output audio label.

        Builds a per-clip audio *bed* concatenated in video order (real audio where the
        clip has it, silence otherwise — keeping the bed synced to the concat), then
        overlays each dedicated audio track (``adelay`` to its ``start_seconds``) via a
        pure ``amix`` (``normalize=0``). No implicit processing (W8.4e.1).
        """
        bed_labels: list[str] = []
        for i, clip in enumerate(spec.inputs):
            if clip_has_audio[i]:
                parts.append(
                    f"[{i}:a]atrim=start={clip.source_start_seconds}:"
                    f"end={clip.source_end_seconds},asetpts=PTS-STARTPTS,"
                    f"volume={clip.volume},{_AFMT}[a{i}]"
                )
            else:
                duration = clip.source_end_seconds - clip.source_start_seconds
                parts.append(
                    f"anullsrc=channel_layout={_A_LAYOUT}:sample_rate={_A_SR},"
                    f"atrim=duration={duration},asetpts=PTS-STARTPTS,{_AFMT}[a{i}]"
                )
            bed_labels.append(f"[a{i}]")
        parts.append("".join(bed_labels) + f"concat=n={n}:v=0:a=1[bed]")

        mix_labels = ["[bed]"]
        for j, audio in enumerate(included_audio):
            idx = n + j
            filt = (
                f"[{idx}:a]atrim=start={audio.source_start_seconds}:"
                f"end={audio.source_end_seconds},asetpts=PTS-STARTPTS,"
                f"volume={audio.volume},{_AFMT}"
            )
            delay_ms = int(round(audio.start_seconds * 1000))
            if delay_ms > 0:
                filt += f",adelay={delay_ms}|{delay_ms}"
            filt += f"[ax{j}]"
            parts.append(filt)
            mix_labels.append(f"[ax{j}]")

        if len(mix_labels) == 1:
            return "[bed]"
        # duration=first bounds the mixed audio to the bed (= composed video length);
        # normalize=0 keeps the mix a pure weighted sum (W8.4e.1 — no implicit gain).
        parts.append(
            "".join(mix_labels) + f"amix=inputs={len(mix_labels)}:duration=first:normalize=0[outa]"
        )
        return "[outa]"

    async def _probe_has_audio(self, path: str) -> bool:
        """Return True iff ``path`` has at least one audio stream (ffprobe)."""
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
