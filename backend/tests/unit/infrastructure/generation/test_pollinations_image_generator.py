"""Unit tests for the thin Pollinations image adapter (httpx MockTransport)."""

from __future__ import annotations

import httpx
import pytest

from app.application.interfaces.image_generator import ImageGenerationError
from app.infrastructure.generation.pollinations_image_generator import PollinationsImageGenerator

pytestmark = pytest.mark.unit

_PNG = b"\x89PNG\r\n\x1a\nFAKEIMAGE"


class _Recorder:
    def __init__(self, handler) -> None:
        self._handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)


def _generator(handler) -> tuple[PollinationsImageGenerator, _Recorder]:
    recorder = _Recorder(handler)
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(recorder),
        base_url="https://image.pollinations.ai",
    )
    return PollinationsImageGenerator(client=client, model="flux"), recorder


async def test_returns_image_bytes_and_metadata() -> None:
    gen, rec = _generator(
        lambda req: httpx.Response(200, content=_PNG, headers={"content-type": "image/png"})
    )
    result = await gen.generate(
        adapter_id="pollinations.image", prompt="a happy shark", seed=42, width=720, height=1280
    )
    assert result.data == _PNG
    assert result.content_type == "image/png"
    assert result.adapter_id == "pollinations.image"
    assert result.provider_id == "pollinations"
    assert result.model == "flux"

    assert len(rec.requests) == 1
    req = rec.requests[0]
    assert "/prompt/" in req.url.path
    assert "a%20happy%20shark" in str(req.url) or "a+happy+shark" in str(req.url)
    assert req.url.params["width"] == "720"
    assert req.url.params["height"] == "1280"
    assert req.url.params["seed"] == "42"
    assert req.url.params["model"] == "flux"
    assert req.url.params["nologo"] == "true"


async def test_http_error_raises_image_generation_error() -> None:
    gen, _ = _generator(lambda req: httpx.Response(500, text="boom"))
    with pytest.raises(ImageGenerationError):
        await gen.generate(adapter_id="pollinations.image", prompt="x", seed=1, width=64, height=64)


async def test_non_image_response_raises() -> None:
    gen, _ = _generator(
        lambda req: httpx.Response(200, text="rate limited", headers={"content-type": "text/plain"})
    )
    with pytest.raises(ImageGenerationError):
        await gen.generate(adapter_id="pollinations.image", prompt="x", seed=1, width=64, height=64)


async def test_negative_prompt_and_refs_are_ignored_not_errored() -> None:
    gen, _ = _generator(
        lambda req: httpx.Response(200, content=_PNG, headers={"content-type": "image/png"})
    )
    # Pollinations doesn't support these; passing them must be a harmless no-op.
    result = await gen.generate(
        adapter_id="pollinations.image",
        prompt="x",
        seed=1,
        width=64,
        height=64,
        negative_prompt="blurry, watermark",
        reference_image_refs=("refs/face.png",),
    )
    assert result.data == _PNG
