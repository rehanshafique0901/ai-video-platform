"""Thin Pollinations image adapter (implements ``IImageGenerator``).

Deliberately minimal: prompt -> HTTP request -> image bytes -> ``GeneratedImage``.
No retries, identity, prompt building, verification, or provider selection live
here — those are owned by the domain and the use case. Pollinations' simple GET
endpoint does not support negative prompts or reference-image conditioning, so
those inputs are accepted (per the port contract) and ignored.

Config-blind (W8.1.1): the ``base_url`` and timeout are injected via the
``httpx.AsyncClient`` at construction; nothing is read from the environment here.
"""

from __future__ import annotations

from urllib.parse import quote

import httpx

from app.application.interfaces.image_generator import (
    GeneratedImage,
    IImageGenerator,
    ImageGenerationError,
)

PROVIDER_ID = "pollinations"


class PollinationsImageGenerator(IImageGenerator):
    def __init__(self, *, client: httpx.AsyncClient, model: str = "flux") -> None:
        self._client = client
        self._model = model

    async def generate(
        self,
        *,
        adapter_id: str,
        prompt: str,
        seed: int,
        width: int,
        height: int,
        negative_prompt: str | None = None,
        reference_image_refs: tuple[str, ...] = (),
        local_model_path: str | None = None,
    ) -> GeneratedImage:
        path = f"/prompt/{quote(prompt, safe='')}"
        params: dict[str, str | int] = {
            "width": width,
            "height": height,
            "seed": seed,
            "nologo": "true",
            "model": self._model,
        }
        try:
            response = await self._client.get(path, params=params)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ImageGenerationError(f"pollinations request failed: {exc}") from exc

        data = response.content
        content_type = response.headers.get("content-type", "image/jpeg")
        if not data or not content_type.startswith("image/"):
            raise ImageGenerationError(
                f"pollinations returned non-image response "
                f"(content-type={content_type!r}, {len(data)} bytes)"
            )
        return GeneratedImage(
            data=data,
            content_type=content_type,
            adapter_id=adapter_id,
            provider_id=PROVIDER_ID,
            model=self._model,
        )
