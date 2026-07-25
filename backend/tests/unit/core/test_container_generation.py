"""Unit tests for α8.6 generation-runtime wiring in the DI container (Increment 3).

Proves the composition root builds the real generation adapters, composes them
into ``GenerateVideo`` over a session, and closes the image HTTP client on
shutdown. No DB connection or network egress happens (a session is created but
never queried; the adapters make no calls at construction).
"""

from __future__ import annotations

import pytest

from app.application.use_cases.generation.generate_video import GenerateVideo
from app.core import container
from app.core.config import Settings
from app.infrastructure.generation.pillow_feature_extractor import PillowFeatureExtractor
from app.infrastructure.generation.pollinations_image_generator import PollinationsImageGenerator
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
        assert isinstance(container._get_image_generator(), PollinationsImageGenerator)
        assert isinstance(container._get_feature_extractor(), PillowFeatureExtractor)
        assert isinstance(container._get_slideshow_renderer(), FfmpegSlideshowRenderer)
        assert isinstance(container._get_video_probe(), FfprobeVideoProbe)
        # memoised (same instance on second call)
        assert container._get_image_generator() is container._get_image_generator()

        session = container.get_session_factory()()
        try:
            use_case = container.get_generate_video_use_case(session)
            assert isinstance(use_case, GenerateVideo)
        finally:
            await session.close()
    finally:
        await container.shutdown()  # must close the image httpx client without error


async def test_generation_settings_defaults() -> None:
    settings = _settings()
    assert settings.pollinations_base_url == "https://image.pollinations.ai"
    assert settings.pollinations_model == "flux"
    assert settings.pollinations_timeout_seconds > 0
