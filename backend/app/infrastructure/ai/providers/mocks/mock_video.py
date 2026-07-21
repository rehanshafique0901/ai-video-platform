"""Deterministic mock video provider — the async job lifecycle (Slice α7.4, α8.3).

Unlike the other three mocks, video does **not** complete inline: :meth:`submit`
returns ``IN_PROGRESS`` with a deterministic ``provider_job_id`` (the α7.6 runner
then pauses + checkpoints), and :meth:`resolve` — added in α8.3 — turns a
previously-submitted job into a deterministic **terminal** ``SUCCEEDED`` result.
Together they let the runtime and its tests exercise the full async
pause → resolve → resume path with no network and no real provider.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.application.interfaces.providers import (
    Capability,
    GenerateVideoRequest,
    GenerateVideoResponse,
    ProviderHealth,
    ProviderMetadata,
    ProviderStatus,
    ProviderUsage,
)

# Deterministic completed-duration the mock reports on resolve (seconds). Fixed so
# tests can assert the recorded terminal usage without any real generation.
_MOCK_COMPLETED_SECONDS = 5


class MockVideoProvider:
    """Async video lifecycle: :meth:`submit` → ``IN_PROGRESS``; :meth:`resolve` → ``SUCCEEDED``."""

    metadata = ProviderMetadata(
        id="mock-video",
        name="Mock Video",
        capability=Capability.VIDEO,
        supports_polling=True,
        supports_webhooks=True,
        version="1.0",
    )

    async def submit(self, req: GenerateVideoRequest) -> GenerateVideoResponse:
        job_id = f"mock-video-job:{req.request_id}"
        return GenerateVideoResponse(
            request_id=req.request_id,
            provider=self.metadata.id,
            status=ProviderStatus.IN_PROGRESS,
            output={"provider_job_id": job_id},
            provider_job_id=job_id,
            usage=ProviderUsage(
                unit="seconds",
                quantity=req.duration_seconds or 0,
            ),
        )

    async def resolve(
        self, *, provider_job_id: str, envelope: Mapping[str, Any]
    ) -> GenerateVideoResponse:
        """Resolve a submitted mock job to a deterministic terminal ``SUCCEEDED`` result.

        The α8.3 completion engine calls this after the run has paused; the returned
        ``usage`` is what the completion path records as the terminal, priced row
        (under the checkpointed ``request_id``, not the empty one here).
        """
        video_ref = f"mock-video://{provider_job_id}"
        return GenerateVideoResponse(
            request_id="",  # the completion engine records under the checkpointed request_id
            provider=self.metadata.id,
            status=ProviderStatus.SUCCEEDED,
            provider_job_id=provider_job_id,
            video_ref=video_ref,
            output={"provider_job_id": provider_job_id, "video_ref": video_ref},
            usage=ProviderUsage(unit="seconds", quantity=_MOCK_COMPLETED_SECONDS),
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth(healthy=True, detail="mock")
