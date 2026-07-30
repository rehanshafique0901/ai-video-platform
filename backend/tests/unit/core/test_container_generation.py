"""Unit tests for α8.6 generation-runtime wiring in the DI container (Increment 3).

Proves the composition root builds the real generation adapters behind the ADR-0054
adapter registry, composes them into ``GenerateVideo`` over a session, and closes every
image HTTP client on shutdown. No DB connection or network egress happens (a session is
created but never queried; the adapters make no calls at construction).
"""

from __future__ import annotations

import pytest

from app.application.interfaces.image_generator import AdapterNotRegisteredError
from app.application.use_cases.generation.generate_video import GenerateVideo
from app.core import container
from app.core.config import Settings
from app.infrastructure.generation.pillow_feature_extractor import PillowFeatureExtractor
from app.infrastructure.generation.pollinations_image_generator import (
    ADAPTER_ID,
    PollinationsImageGenerator,
)
from app.infrastructure.generation.registry import (
    IMPLEMENTED_IMAGE_ADAPTER_IDS,
    ImageAdapterRegistry,
)
from app.infrastructure.render.ffmpeg_slideshow_renderer import FfmpegSlideshowRenderer
from app.infrastructure.render.ffprobe_video_probe import FfprobeVideoProbe

pytestmark = pytest.mark.unit

_JWT = "test-secret-do-not-use-in-production-32chars"


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "database_url": "postgresql+psycopg://u:p@h:5432/d",
        "jwt_secret": _JWT,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg,arg-type]


async def test_generation_adapters_wire_and_compose() -> None:
    container.reset()
    container.init(_settings())
    try:
        registry = container._get_image_adapter_registry()
        assert isinstance(registry, ImageAdapterRegistry)
        assert isinstance(registry.for_adapter(ADAPTER_ID), PollinationsImageGenerator)
        assert isinstance(container._get_feature_extractor(), PillowFeatureExtractor)
        assert isinstance(container._get_slideshow_renderer(), FfmpegSlideshowRenderer)
        assert isinstance(container._get_video_probe(), FfprobeVideoProbe)
        # memoised (same instance on second call)
        assert container._get_image_adapter_registry() is registry

        session = container.get_session_factory()()
        try:
            use_case = container.get_generate_video_use_case(session)
            assert isinstance(use_case, GenerateVideo)
        finally:
            await session.close()
    finally:
        await container.shutdown()  # must close every image adapter client without error


async def test_registry_keys_are_the_executable_set() -> None:
    # ADR-0054 D1: the composition root's registry keys *are* what the resolver is told
    # this deployment can construct. They must equal the declared implemented set, which
    # is the half of catalogue/code reconciliation that provider validation cannot see.
    container.reset()
    container.init(_settings())
    try:
        keys = container._get_image_adapter_registry().supported_adapters()
        assert keys == frozenset({ADAPTER_ID})
        assert keys == IMPLEMENTED_IMAGE_ADAPTER_IDS
    finally:
        await container.shutdown()


async def test_unregistered_adapter_is_a_permanent_failure() -> None:
    container.reset()
    container.init(_settings())
    try:
        with pytest.raises(AdapterNotRegisteredError) as excinfo:
            container._get_image_adapter_registry().for_adapter("comfyui.flux_schnell")
        assert excinfo.value.retryable is False
    finally:
        await container.shutdown()


async def test_shutdown_closes_every_image_adapter_client() -> None:
    container.reset()
    container.init(_settings())
    container._get_image_adapter_registry()
    clients = list(container._image_adapter_clients)
    assert clients, "constructing the registry must register its clients for shutdown"
    await container.shutdown()
    assert all(c.is_closed for c in clients)
    assert container._image_adapter_clients == []


async def test_generation_settings_defaults() -> None:
    settings = _settings()
    assert settings.pollinations_base_url == "https://image.pollinations.ai"
    assert settings.pollinations_model == "flux"
    assert settings.pollinations_timeout_seconds > 0
