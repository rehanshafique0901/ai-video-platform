"""OpenAI provider adapters — the first real capability (Slice α8.1).

α8.1 ships **one** adapter: the synchronous :class:`OpenAIImageProvider` for
``Capability.IMAGE``. It lives in the strict provider leaf
(``app.infrastructure.ai.providers``) — implementing the neutral
``ImageProvider`` contract with nothing but ``httpx`` and the neutral DTOs — so
no orchestration layer (runner / dispatcher / recorder / relay / lock manager)
changes when a real provider replaces the mock.
"""

from __future__ import annotations

from app.infrastructure.ai.providers.openai.image import OpenAIImageProvider

__all__ = ["OpenAIImageProvider"]
