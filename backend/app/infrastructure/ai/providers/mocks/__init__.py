"""Deterministic, network-free mock providers — one per capability (Slice α7.4).

Each mock returns a fixed :class:`ProviderResponse` derived only from its request
(byte-reproducible, no I/O), so the whole runtime runs with no broker and no
external API through α7.6. The **video** mock models the async path
(``IN_PROGRESS`` + a deterministic ``provider_job_id``) so the completion shape
(α8.3) exists and is tested before any real async provider arrives.
"""

from __future__ import annotations

from app.infrastructure.ai.providers.mocks.mock_image import MockImageProvider
from app.infrastructure.ai.providers.mocks.mock_llm import MockLLMProvider
from app.infrastructure.ai.providers.mocks.mock_video import MockVideoProvider
from app.infrastructure.ai.providers.mocks.mock_voice import MockVoiceProvider

__all__ = [
    "MockLLMProvider",
    "MockImageProvider",
    "MockVideoProvider",
    "MockVoiceProvider",
]
