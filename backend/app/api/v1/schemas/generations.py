"""DTOs for ``/api/v1/generations/*`` — the α9.7 generation ingress (ADR-0052 D3/D5).

* :class:`GenerationCreateRequest` — what a creator asks for. Flat and scalar, plus an optional
  ``identity_id`` (α10.0) naming one of the caller's authored worlds. The world itself is never
  in the body: it is read once at acceptance and snapshotted into the stored request, so the
  cast a generation runs with cannot change under it (ADR-0055 IDENT-1). Reference images
  remain out of scope — no executable adapter consumes them (PF5).
* :class:`GenerationPublic` — the **curated** read projection. Field selection is deliberate
  and load-bearing: ``provenance``, the resolution ledger, the chosen adapter/provider, every
  component version, and ``final_video_asset_id`` are execution internals and are *not*
  declared here, so they cannot reach the wire by accident. That is the ADR-0051 read-model
  hygiene lesson (the ``_email`` precedent) applied to a second bounded context.

``promotable`` replaces the raw ``final_video_asset_id``: the client's next action is
``POST /media/promotions`` with the ``generation_id`` it already holds, so the internal asset id
has no reason to cross the boundary.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.domain.generation.execution import ExecutionMode
from app.domain.generation.identity import GlobalStyle


class GenerationCreateRequest(BaseModel):
    """``POST /generations`` body. Only ``prompt`` is required; the rest mirror the runtime
    request's own defaults, so an otherwise-empty body produces the platform's standard
    vertical short-form video.

    ``idempotency_key`` is the creator's *explicit* replay intent. It is deliberately not
    derived from the prompt: asking twice for the same prompt is a legitimate second take, and
    content-hashing would silently deny it (ADR-0052 D4).

    ``seed`` and ``global_style`` are optional so that "the caller said nothing" stays
    distinguishable from "the caller asked for the default": when a world is named, the value
    it declares fills the gap, and a value stated here outranks it (ADR-0055 D4).
    """

    prompt: str = Field(min_length=1, max_length=2000)
    idempotency_key: str | None = Field(default=None, max_length=255)
    title: str | None = Field(default=None, max_length=200)
    execution_mode: ExecutionMode = ExecutionMode.AUTO
    global_style: GlobalStyle | None = Field(
        default=None,
        description=(
            "Omit to inherit the named world's style, or the platform default "
            f"({GlobalStyle.PIXAR.value}) when no world is named."
        ),
    )
    aspect_ratio: str = Field(default="9:16", max_length=16)
    target_platform: str = Field(default="reel", max_length=32)
    target_duration_seconds: float = Field(default=18.0, gt=0, le=300)
    per_shot_seconds: float = Field(default=3.0, gt=0, le=60)
    width: int = Field(default=720, ge=64, le=4096)
    height: int = Field(default=1280, ge=64, le=4096)
    fps: int = Field(default=30, ge=1, le=60)
    seed: int | None = Field(default=None, ge=0, lt=2**31)
    identity_id: UUID | None = Field(
        default=None,
        description=(
            "One of the caller's identity profiles. Its world is snapshotted into this "
            "request at acceptance; later edits to the profile never reach this generation."
        ),
    )


class GenerationPublic(BaseModel):
    """The owner-facing projection of a generation.

    ``shots_accepted`` / ``shot_count`` is the coarse progress signal for a polling client;
    ``shot_count`` is ``None`` until planning has run. ``promotable`` is true once the
    generation has completed *and* registered a final video, i.e. once promotion will succeed.
    """

    id: UUID
    status: str
    prompt: str
    title: str | None
    aspect_ratio: str | None
    target_platform: str | None
    width: int | None
    height: int | None
    fps: int | None
    shot_count: int | None
    shots_accepted: int
    duration_seconds: float | None
    failure_reason: str | None
    promotable: bool
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
