"""Unit tests for the neutral provider contract (Slice α7.4).

The DTOs / enums / metadata / errors in ``app.application.interfaces.providers``
are the boundary every provider implements against. Coverage:

* C1 — request/response/usage/metadata dataclasses are frozen (immutable).
* C2 — ``Capability`` / ``ProviderStatus`` enum values are the stable wire strings.
* C3 — the error hierarchy: all subclass ``ProviderError``; transient vs terminal
  classification matches ADR-0041 D10 (unavailable/rate-limited/timeout = transient;
  auth/validation/no-provider = terminal).
* C4 — per-capability responses carry the common envelope (subclass ``ProviderResponse``).
"""

from __future__ import annotations

import dataclasses

import pytest

from app.application.interfaces.providers import (
    Capability,
    GenerateImageRequest,
    GenerateImageResponse,
    GenerateSpeechResponse,
    GenerateTextResponse,
    GenerateVideoResponse,
    NoProviderAvailable,
    ProviderAuthenticationError,
    ProviderError,
    ProviderMetadata,
    ProviderRateLimited,
    ProviderResponse,
    ProviderStatus,
    ProviderTimeout,
    ProviderUnavailable,
    ProviderUsage,
    ProviderValidationError,
)


def test_c1_dtos_are_frozen() -> None:
    req = GenerateImageRequest(request_id="r1", prompt="hi")
    with pytest.raises(dataclasses.FrozenInstanceError):
        req.prompt = "changed"  # type: ignore[misc]

    usage = ProviderUsage(unit="images", quantity=1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        usage.quantity = 2  # type: ignore[misc]

    meta = ProviderMetadata(
        id="mock-image",
        name="Mock Image",
        capability=Capability.IMAGE,
        supports_polling=False,
        supports_webhooks=False,
        version="1.0",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        meta.version = "2.0"  # type: ignore[misc]


def test_c2_enum_wire_values() -> None:
    assert [c.value for c in Capability] == ["llm", "image", "video", "voice"]
    assert ProviderStatus.SUCCEEDED.value == "succeeded"
    assert ProviderStatus.IN_PROGRESS.value == "in_progress"
    assert ProviderStatus.FAILED.value == "failed"


def test_c3_error_hierarchy_and_transience() -> None:
    transient = [ProviderUnavailable, ProviderRateLimited, ProviderTimeout]
    terminal = [ProviderAuthenticationError, ProviderValidationError, NoProviderAvailable]

    for err in transient + terminal:
        assert issubclass(err, ProviderError)

    assert all(err.transient is True for err in transient)
    assert all(err.transient is False for err in terminal)
    assert ProviderError.transient is False


def test_c4_capability_responses_share_the_envelope() -> None:
    for cls in (
        GenerateTextResponse,
        GenerateImageResponse,
        GenerateVideoResponse,
        GenerateSpeechResponse,
    ):
        assert issubclass(cls, ProviderResponse)

    resp = GenerateImageResponse(
        request_id="r1",
        provider="mock-image",
        status=ProviderStatus.SUCCEEDED,
        image_ref="mock://image/r1",
    )
    assert resp.request_id == "r1"
    assert resp.provider == "mock-image"
    assert resp.status is ProviderStatus.SUCCEEDED
    assert resp.image_ref == "mock://image/r1"
    assert resp.provider_job_id is None
