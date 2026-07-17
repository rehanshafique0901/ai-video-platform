"""Unit tests for ``StepCommandDispatcher`` (Slice α7.4).

The dispatcher is the closed ``StepCommand.kind`` → capability mapping table
(ADR-0041 D4). Coverage:

* D1 — each of the four supported kinds routes to the matching capability call and
  threads the response (``request_id`` / provider / status).
* D2 — ``generate_video`` surfaces the async ``IN_PROGRESS`` + ``provider_job_id``.
* D3 — an excluded/unknown kind (``start_render``) raises ``ProviderValidationError``
  (render/export/storage are out of scope, Q6).
* D4 — a missing ``request_id`` raises ``ProviderValidationError`` (terminal, D1/D13).
* D5 — ``NoProviderAvailable`` propagates when the capability is unregistered.
* D6 — discovery (``supports`` / ``list_capabilities``) delegates to the registry.
"""

from __future__ import annotations

import pytest

from app.application.interfaces.providers import (
    Capability,
    NoProviderAvailable,
    ProviderStatus,
    ProviderValidationError,
)
from app.domain.workflow.registry import StepCommand
from app.infrastructure.ai.dispatcher import StepCommandDispatcher
from app.infrastructure.ai.providers.mocks import MockImageProvider
from app.infrastructure.ai.providers.registry import ProviderRegistry, default_registry


def _dispatcher() -> StepCommandDispatcher:
    return StepCommandDispatcher(default_registry())


async def test_d1_routes_each_kind() -> None:
    disp = _dispatcher()

    text = await disp.dispatch(
        StepCommand(kind="generate_text", args={"request_id": "r1", "prompt": "hi"})
    )
    assert text.provider == "mock-llm"
    assert text.request_id == "r1"
    assert text.status is ProviderStatus.SUCCEEDED

    image = await disp.dispatch(
        StepCommand(kind="generate_image", args={"request_id": "r2", "prompt": "a cat"})
    )
    assert image.provider == "mock-image"

    voice = await disp.dispatch(
        StepCommand(kind="synthesize_voice", args={"request_id": "r3", "text": "hi"})
    )
    assert voice.provider == "mock-voice"


async def test_d2_video_async() -> None:
    disp = _dispatcher()
    resp = await disp.dispatch(
        StepCommand(
            kind="generate_video",
            args={"request_id": "r4", "prompt": "a dog", "duration_seconds": 3},
        )
    )
    assert resp.status is ProviderStatus.IN_PROGRESS
    assert resp.provider_job_id == "mock-video-job:r4"


async def test_d3_excluded_kind_raises() -> None:
    disp = _dispatcher()
    with pytest.raises(ProviderValidationError):
        await disp.dispatch(StepCommand(kind="start_render", args={"request_id": "r5"}))
    with pytest.raises(ProviderValidationError):
        await disp.dispatch(StepCommand(kind="not_a_capability", args={"request_id": "r6"}))


async def test_d4_missing_request_id_raises() -> None:
    disp = _dispatcher()
    with pytest.raises(ProviderValidationError):
        await disp.dispatch(StepCommand(kind="generate_image", args={"prompt": "a cat"}))


async def test_d5_no_provider_available_propagates() -> None:
    # A registry with only image registered → a text command finds no provider.
    registry = ProviderRegistry()
    registry.register(provider=MockImageProvider(), capabilities=[Capability.IMAGE])
    disp = StepCommandDispatcher(registry)
    with pytest.raises(NoProviderAvailable):
        await disp.dispatch(
            StepCommand(kind="generate_text", args={"request_id": "r7", "prompt": "hi"})
        )


def test_d6_discovery_delegates() -> None:
    disp = _dispatcher()
    assert disp.supports(Capability.IMAGE) is True
    assert disp.list_capabilities() == [
        Capability.LLM,
        Capability.IMAGE,
        Capability.VIDEO,
        Capability.VOICE,
    ]
