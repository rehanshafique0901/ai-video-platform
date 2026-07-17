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

from typing import Protocol, runtime_checkable

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
    """Video generation (``Capability.VIDEO``) — async: returns ``IN_PROGRESS`` + job id."""

    async def generate_video(self, req: GenerateVideoRequest) -> GenerateVideoResponse: ...


class VoiceProvider(Provider, Protocol):
    """Speech synthesis (``Capability.VOICE``)."""

    async def synthesize_voice(self, req: GenerateSpeechRequest) -> GenerateSpeechResponse: ...
