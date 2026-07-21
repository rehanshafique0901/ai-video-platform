"""Capability protocols — the provider leaf layer (Slice α7.4, ADR-0041 D1).

A provider is addressed only through one of four narrow, typed, **async**
capability protocols keyed by :class:`Capability`. Each takes a neutral request
dataclass and returns a neutral response dataclass (or raises a typed
:class:`ProviderError`) — SDK types never leak upward.

This package (``app.infrastructure.ai.providers``) is a **strict leaf**
(``import-linter``-enforced): it imports only the neutral contract in
``app.application.interfaces.providers`` (the DTOs / errors / metadata every
provider implements against) and nothing from ``app.application.use_cases``,
``app.api``, or the workflow domain. The ``StepCommandDispatcher`` that bridges
``StepCommand`` → these protocols lives one level up (``app/infrastructure/ai/
dispatcher.py``), outside the leaf, precisely so this layer stays orchestration-free.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from app.application.interfaces.providers import (
    GenerateImageRequest,
    GenerateImageResponse,
    GenerateSpeechRequest,
    GenerateSpeechResponse,
    GenerateTextRequest,
    GenerateTextResponse,
    GenerateVideoRequest,
    GenerateVideoResponse,
    ProviderHealth,
    ProviderMetadata,
)


@runtime_checkable
class Provider(Protocol):
    """Common surface of every provider: immutable metadata + a health probe."""

    metadata: ProviderMetadata

    async def health(self) -> ProviderHealth: ...


class LLMProvider(Provider, Protocol):
    """Text generation (``Capability.LLM``)."""

    async def generate_text(self, req: GenerateTextRequest) -> GenerateTextResponse: ...


class ImageProvider(Provider, Protocol):
    """Image generation (``Capability.IMAGE``)."""

    async def generate_image(self, req: GenerateImageRequest) -> GenerateImageResponse: ...


class VideoProvider(Provider, Protocol):
    """Video generation (``Capability.VIDEO``) — an **async job lifecycle** (α8.3).

    Every async provider (Fal, Runway, Kling, Pika, Luma, …) shares the same
    lifecycle — ``submit → job id → poll/webhook → result`` — so the capability is
    modelled as two verbs rather than a provider-specific ``get_video_result``:

    * :meth:`submit` — start the job; returns ``IN_PROGRESS`` + a ``provider_job_id``
      (the runner then pauses and checkpoints the resume coordinates). This is the
      α8.2 submit call, renamed from ``generate_video`` under the lifecycle.
    * :meth:`resolve` — resolve a previously-submitted job to a **terminal**
      ``SUCCEEDED`` / ``FAILED`` (or still ``IN_PROGRESS``) result, given the
      checkpointed ``provider_job_id`` + the opaque ``envelope`` (Fal
      ``status_url`` / ``response_url``). Called by the α8.3 completion engine —
      **never** by the dispatcher (the submit path). Provider-agnostic: returns the
      same neutral :class:`GenerateVideoResponse` DTO.
    """

    async def submit(self, req: GenerateVideoRequest) -> GenerateVideoResponse: ...

    async def resolve(
        self, *, provider_job_id: str, envelope: Mapping[str, Any]
    ) -> GenerateVideoResponse: ...


class VoiceProvider(Provider, Protocol):
    """Speech synthesis (``Capability.VOICE``)."""

    async def synthesize_voice(self, req: GenerateSpeechRequest) -> GenerateSpeechResponse: ...
