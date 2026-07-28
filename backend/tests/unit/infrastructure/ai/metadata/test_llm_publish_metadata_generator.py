"""Unit tests for ``LlmPublishMetadataGenerator`` (α9.1, ADR-0049).

Prove the AI adapter contract in isolation: deterministic behaviour over the default mock LLM,
cap enforcement, graceful mapping of every provider failure mode to
:class:`PublishMetadataGenerationError`, timeout handling, and cancellation safety (the underlying
provider call is cancelled cleanly, ``CancelledError`` propagates, no leaked task).
"""

from __future__ import annotations

import asyncio

import pytest

from app.application.interfaces.providers import (
    Capability,
    GenerateTextRequest,
    GenerateTextResponse,
    ProviderHealth,
    ProviderMetadata,
    ProviderStatus,
    ProviderUnavailable,
)
from app.application.interfaces.publish_metadata_generator import (
    PublishMetadataGenerationError,
    PublishMetadataRequest,
)
from app.infrastructure.ai.metadata.llm_publish_metadata_generator import (
    LlmPublishMetadataGenerator,
)
from app.infrastructure.ai.providers.registry import ProviderRegistry, default_registry

pytestmark = pytest.mark.unit

_META = ProviderMetadata(
    id="stub-llm",
    name="Stub LLM",
    capability=Capability.LLM,
    supports_polling=False,
    supports_webhooks=False,
    version="1.0",
)


def _req(
    context: str = "My cool travel video",
    *,
    max_title: int = 100,
    max_desc: int = 5000,
    max_tags_total: int = 500,
    max_tag_count: int = 15,
) -> PublishMetadataRequest:
    return PublishMetadataRequest(
        request_id="req-1",
        context=context,
        max_title_chars=max_title,
        max_description_chars=max_desc,
        max_tags_total_chars=max_tags_total,
        max_tag_count=max_tag_count,
    )


class _StubProvider:
    """Configurable LLM provider stub."""

    metadata = _META

    def __init__(
        self,
        *,
        response: GenerateTextResponse | None = None,
        exc: Exception | None = None,
        sleep: float | None = None,
    ) -> None:
        self._response = response
        self._exc = exc
        self._sleep = sleep
        self.cancelled = False

    async def generate_text(self, req: GenerateTextRequest) -> GenerateTextResponse:
        if self._sleep is not None:
            try:
                await asyncio.sleep(self._sleep)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        if self._exc is not None:
            raise self._exc
        assert self._response is not None
        return self._response

    async def health(self) -> ProviderHealth:
        return ProviderHealth(healthy=True, detail="stub")


def _registry_with(provider: object) -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(provider=provider, capabilities=[Capability.LLM])  # type: ignore[arg-type]
    return registry


def _gen(registry: ProviderRegistry, *, timeout: float = 5.0) -> LlmPublishMetadataGenerator:
    return LlmPublishMetadataGenerator(registry, timeout_seconds=timeout)


# ---- deterministic behaviour over the default mock -------------------------


async def test_default_mock_is_deterministic() -> None:
    gen = _gen(default_registry())
    first = await gen.generate(_req())
    second = await gen.generate(_req())
    assert first == second
    assert first.provenance.generator == "llm"
    assert first.provenance.is_fallback is False
    assert first.title  # non-empty
    assert first.provenance.prompt_template_version


async def test_output_respects_caps() -> None:
    gen = _gen(default_registry())
    result = await gen.generate(
        _req("word " * 50, max_title=10, max_desc=20, max_tag_count=3, max_tags_total=100)
    )
    assert len(result.title) <= 10
    assert len(result.description) <= 20
    assert len(result.tags) <= 3


async def test_parses_title_description_tags_from_completion() -> None:
    resp = GenerateTextResponse(
        request_id="req-1",
        provider=_META.id,
        status=ProviderStatus.SUCCEEDED,
        text="Sunrise hiking adventure\n\nchase waterfalls and mountain trails",
    )
    gen = _gen(_registry_with(_StubProvider(response=resp)))
    result = await gen.generate(_req())
    assert result.title == "Sunrise hiking adventure"
    assert "hiking" in result.description
    assert "waterfalls" not in result.tags  # only the first paragraph feeds tags
    assert "hiking" in result.tags


# ---- graceful degradation: every failure maps to the domain error ----------


async def test_provider_error_maps_to_generation_error() -> None:
    gen = _gen(_registry_with(_StubProvider(exc=ProviderUnavailable("down"))))
    with pytest.raises(PublishMetadataGenerationError):
        await gen.generate(_req())


async def test_no_provider_registered_maps_to_generation_error() -> None:
    # An empty registry → resolve raises NoProviderAvailable (a ProviderError) → domain error.
    gen = _gen(ProviderRegistry())
    with pytest.raises(PublishMetadataGenerationError):
        await gen.generate(_req())


async def test_non_terminal_response_maps_to_generation_error() -> None:
    resp = GenerateTextResponse(
        request_id="req-1", provider=_META.id, status=ProviderStatus.FAILED, text="", error="x"
    )
    gen = _gen(_registry_with(_StubProvider(response=resp)))
    with pytest.raises(PublishMetadataGenerationError):
        await gen.generate(_req())


async def test_empty_text_maps_to_generation_error() -> None:
    resp = GenerateTextResponse(
        request_id="req-1", provider=_META.id, status=ProviderStatus.SUCCEEDED, text="   "
    )
    gen = _gen(_registry_with(_StubProvider(response=resp)))
    with pytest.raises(PublishMetadataGenerationError):
        await gen.generate(_req())


async def test_empty_title_maps_to_generation_error() -> None:
    # A bare bracket prefix strips to an empty body → empty title → domain error.
    resp = GenerateTextResponse(
        request_id="req-1", provider=_META.id, status=ProviderStatus.SUCCEEDED, text="[tag]"
    )
    gen = _gen(_registry_with(_StubProvider(response=resp)))
    with pytest.raises(PublishMetadataGenerationError):
        await gen.generate(_req())


async def test_timeout_maps_to_generation_error() -> None:
    provider = _StubProvider(sleep=1.0)
    gen = _gen(_registry_with(provider), timeout=0.01)
    with pytest.raises(PublishMetadataGenerationError):
        await gen.generate(_req())
    # asyncio.wait_for cancelled the underlying provider call — no leaked task.
    assert provider.cancelled is True


# ---- cancellation safety ---------------------------------------------------


async def test_cancellation_propagates_and_cancels_provider() -> None:
    provider = _StubProvider(sleep=3600.0)
    gen = _gen(_registry_with(provider), timeout=3600.0)

    task = asyncio.create_task(gen.generate(_req()))
    await asyncio.sleep(0.05)  # let it reach the provider await
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The inner provider call was cancelled cleanly (no background task left running).
    assert provider.cancelled is True
