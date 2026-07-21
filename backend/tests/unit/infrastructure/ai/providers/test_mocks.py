"""Unit tests for the deterministic mock providers (Slice α7.4).

Coverage:

* M1 — each mock exposes correct :class:`ProviderMetadata` (id / capability /
  polling+webhook flags).
* M2 — LLM / image / voice return ``SUCCEEDED`` inline with a deterministic ref +
  usage derived only from the request (byte-reproducible, no I/O).
* M3 — the video mock models the async path: ``IN_PROGRESS`` + a deterministic
  ``provider_job_id`` (nothing completes it in α7.4).
* M4 — ``health()`` reports healthy for every mock.
"""

from __future__ import annotations

from app.application.interfaces.providers import (
    Capability,
    GenerateImageRequest,
    GenerateSpeechRequest,
    GenerateTextRequest,
    GenerateVideoRequest,
    ProviderStatus,
)
from app.infrastructure.ai.providers.mocks import (
    MockImageProvider,
    MockLLMProvider,
    MockVideoProvider,
    MockVoiceProvider,
)


async def test_m1_metadata() -> None:
    assert MockLLMProvider().metadata.capability is Capability.LLM
    assert MockImageProvider().metadata.id == "mock-image"
    video_meta = MockVideoProvider().metadata
    assert video_meta.capability is Capability.VIDEO
    # video is the async capability → advertises polling + webhooks
    assert video_meta.supports_polling is True
    assert video_meta.supports_webhooks is True
    assert MockImageProvider().metadata.supports_polling is False


async def test_m2_sync_mocks_succeed_deterministically() -> None:
    llm = await MockLLMProvider().generate_text(
        GenerateTextRequest(request_id="r-llm", prompt="hello world")
    )
    assert llm.status is ProviderStatus.SUCCEEDED
    assert llm.text == "[mock-llm] hello world"
    assert llm.usage is not None and llm.usage.unit == "tokens" and llm.usage.quantity == 2

    img = await MockImageProvider().generate_image(
        GenerateImageRequest(request_id="r-img", prompt="a cat")
    )
    assert img.status is ProviderStatus.SUCCEEDED
    assert img.image_ref == "mock://image/r-img"
    assert img.provider_job_id is None

    voice = await MockVoiceProvider().synthesize_voice(
        GenerateSpeechRequest(request_id="r-voice", text="hi")
    )
    assert voice.status is ProviderStatus.SUCCEEDED
    assert voice.audio_ref == "mock://audio/r-voice"
    assert voice.usage is not None and voice.usage.quantity == 2  # len("hi")


async def test_m2_mocks_are_reproducible() -> None:
    req = GenerateImageRequest(request_id="r-img", prompt="a cat")
    first = await MockImageProvider().generate_image(req)
    second = await MockImageProvider().generate_image(req)
    assert first == second


async def test_m3_video_mock_models_async() -> None:
    resp = await MockVideoProvider().submit(
        GenerateVideoRequest(request_id="r-vid", prompt="a dog", duration_seconds=7)
    )
    assert resp.status is ProviderStatus.IN_PROGRESS
    assert resp.provider_job_id == "mock-video-job:r-vid"
    assert resp.usage is not None and resp.usage.unit == "seconds" and resp.usage.quantity == 7


async def test_m3_video_mock_resolves_to_terminal_success() -> None:
    resp = await MockVideoProvider().resolve(provider_job_id="mock-video-job:r-vid", envelope={})
    assert resp.status is ProviderStatus.SUCCEEDED
    assert resp.provider_job_id == "mock-video-job:r-vid"
    assert resp.video_ref == "mock-video://mock-video-job:r-vid"
    # Deterministic terminal usage the α8.3 completion path records (fixed seconds).
    assert resp.usage is not None
    assert resp.usage.unit == "seconds"
    assert resp.usage.quantity == 5


async def test_m4_health() -> None:
    for provider in (
        MockLLMProvider(),
        MockImageProvider(),
        MockVideoProvider(),
        MockVoiceProvider(),
    ):
        health = await provider.health()
        assert health.healthy is True
