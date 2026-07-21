"""Fal.ai provider adapters — the first real *async* capability (Slice α8.2).

α8.2 ships **one** adapter: the asynchronous :class:`FalVideoProvider` for
``Capability.VIDEO``. It lives in the strict provider leaf
(``app.infrastructure.ai.providers``) — implementing the neutral
``VideoProvider`` contract with nothing but ``httpx`` and the neutral DTOs — so
no orchestration layer (runner / dispatcher / recorder / relay / lock manager)
changes when a real provider replaces the mock. The adapter **submits** a queue
job and returns ``IN_PROGRESS`` + a ``provider_job_id``, driving the α7.6 pause
seam; completion (poll / webhook / resume / usage) is α8.3.
"""

from __future__ import annotations

from app.infrastructure.ai.providers.fal.video import FalVideoProvider

__all__ = ["FalVideoProvider"]
