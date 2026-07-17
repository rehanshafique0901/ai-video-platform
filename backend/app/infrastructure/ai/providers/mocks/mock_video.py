"""Deterministic mock video provider — models the async completion path (Slice α7.4).

Unlike the other three mocks, video does **not** complete inline: it returns
``IN_PROGRESS`` with a deterministic ``provider_job_id``. Nothing resolves the job
in α7.4 — the completion service (webhook + polling) is α8.3 — but the shape lets
the runtime and its tests exercise the async branch before any real async provider.
"""

from __future__ import annotations

from app.application.interfaces.providers import (
    Capability,
    GenerateVideoRequest,
    GenerateVideoResponse,
    ProviderHealth,
    ProviderMetadata,
    ProviderStatus,
    ProviderUsage,
)


class MockVideoProvider:
    """Submits an async job: returns ``IN_PROGRESS`` + a deterministic job id."""

    metadata = ProviderMetadata(
        id="mock-video",
        name="Mock Video",
        capability=Capability.VIDEO,
        supports_polling=True,
        supports_webhooks=True,
        version="1.0",
    )

    async def generate_video(self, req: GenerateVideoRequest) -> GenerateVideoResponse:
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

    async def health(self) -> ProviderHealth:
        return ProviderHealth(healthy=True, detail="mock")
