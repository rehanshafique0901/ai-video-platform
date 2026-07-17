"""Deterministic mock voice provider (Slice α7.4)."""

from __future__ import annotations

from app.application.interfaces.providers import (
    Capability,
    GenerateSpeechRequest,
    GenerateSpeechResponse,
    ProviderHealth,
    ProviderMetadata,
    ProviderStatus,
    ProviderUsage,
)


class MockVoiceProvider:
    """Returns a deterministic audio reference inline (``SUCCEEDED``). No network."""

    metadata = ProviderMetadata(
        id="mock-voice",
        name="Mock Voice",
        capability=Capability.VOICE,
        supports_polling=False,
        supports_webhooks=False,
        version="1.0",
    )

    async def synthesize_voice(self, req: GenerateSpeechRequest) -> GenerateSpeechResponse:
        audio_ref = f"mock://audio/{req.request_id}"
        return GenerateSpeechResponse(
            request_id=req.request_id,
            provider=self.metadata.id,
            status=ProviderStatus.SUCCEEDED,
            output={"audio_ref": audio_ref, "voice": req.voice},
            usage=ProviderUsage(unit="characters", quantity=len(req.text)),
            audio_ref=audio_ref,
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth(healthy=True, detail="mock")
