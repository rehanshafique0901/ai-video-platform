"""Deterministic mock image provider (Slice α7.4)."""

from __future__ import annotations

from app.application.interfaces.providers import (
    Capability,
    GenerateImageRequest,
    GenerateImageResponse,
    ProviderHealth,
    ProviderMetadata,
    ProviderStatus,
    ProviderUsage,
)


class MockImageProvider:
    """Returns a deterministic image reference inline (``SUCCEEDED``). No network."""

    metadata = ProviderMetadata(
        id="mock-image",
        name="Mock Image",
        capability=Capability.IMAGE,
        supports_polling=False,
        supports_webhooks=False,
        version="1.0",
    )

    async def generate_image(self, req: GenerateImageRequest) -> GenerateImageResponse:
        image_ref = f"mock://image/{req.request_id}"
        return GenerateImageResponse(
            request_id=req.request_id,
            provider=self.metadata.id,
            status=ProviderStatus.SUCCEEDED,
            output={"image_ref": image_ref, "size": req.size},
            usage=ProviderUsage(unit="images", quantity=1),
            image_ref=image_ref,
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth(healthy=True, detail="mock")
