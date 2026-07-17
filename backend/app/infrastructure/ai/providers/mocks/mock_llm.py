"""Deterministic mock LLM provider (Slice α7.4)."""

from __future__ import annotations

from app.application.interfaces.providers import (
    Capability,
    GenerateTextRequest,
    GenerateTextResponse,
    ProviderHealth,
    ProviderMetadata,
    ProviderStatus,
    ProviderUsage,
)


class MockLLMProvider:
    """Returns a fixed echo completion inline (``SUCCEEDED``). No network, no state."""

    metadata = ProviderMetadata(
        id="mock-llm",
        name="Mock LLM",
        capability=Capability.LLM,
        supports_polling=False,
        supports_webhooks=False,
        version="1.0",
    )

    async def generate_text(self, req: GenerateTextRequest) -> GenerateTextResponse:
        text = f"[mock-llm] {req.prompt}"
        return GenerateTextResponse(
            request_id=req.request_id,
            provider=self.metadata.id,
            status=ProviderStatus.SUCCEEDED,
            output={"text": text},
            usage=ProviderUsage(unit="tokens", quantity=len(req.prompt.split())),
            text=text,
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth(healthy=True, detail="mock")
