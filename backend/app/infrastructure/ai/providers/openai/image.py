"""OpenAI Images provider — the first real capability adapter (Slice α8.1).

A **synchronous** adapter over the OpenAI ``POST /images/generations`` endpoint
that implements the :class:`~app.infrastructure.ai.providers.ports.ImageProvider`
protocol. It is a strict leaf module: it imports only ``httpx`` and the neutral
provider DTOs/errors — never the runner, dispatcher, recorder, workflow domain,
or any config/secret source.

Signed-off invariants this module upholds (see
``docs/engineering/PHASE3_ALPHA8_1_PREFLIGHT.md``):

* **W8.1.1 — configuration-blind.** The adapter receives a fully-configured,
  pre-authenticated ``httpx.AsyncClient`` (base URL, ``Authorization`` header,
  timeout all baked in by the DI layer). It performs **no** env / DB / filesystem
  / vault lookup and never sees the raw API key (Q4: *constructors receive
  secrets, they never retrieve them* — here it does not even receive the secret,
  only a client that already carries it).
* **W8.1.2 — one real capability.** IMAGE only; LLM / VIDEO / VOICE stay mock.
* **W8.1.3 — observational equivalence.** Returns the *same* DTO
  (:class:`GenerateImageResponse`) with the *same* populated field-set and the
  *same* ``SUCCEEDED`` semantics as ``MockImageProvider``; the runner cannot tell
  which produced the response — only the values (real URL, provider id) differ.
* **W7.6.2 — one dispatch, no internal retry.** Exactly one HTTP request per
  call; every transient failure is raised as a transient
  :class:`ProviderError` so the *runner* re-dispatches under the same
  deterministic ``request_id``.

Error mapping (Q7), HTTP status → neutral error, nothing HTTP leaks upward:

===========================  =============================  =========
Condition                    Error                          Class
===========================  =============================  =========
401 / 403                    ``ProviderAuthenticationError``  terminal
400 / other 4xx / policy     ``ProviderValidationError``      terminal
429                          ``ProviderRateLimited``          transient
5xx / connection             ``ProviderUnavailable``          transient
timeout                      ``ProviderTimeout``              transient
200                          ``GenerateImageResponse``        —
===========================  =============================  =========
"""

from __future__ import annotations

from typing import Any

import httpx

from app.application.interfaces.providers import (
    Capability,
    GenerateImageRequest,
    GenerateImageResponse,
    ProviderAuthenticationError,
    ProviderHealth,
    ProviderMetadata,
    ProviderRateLimited,
    ProviderStatus,
    ProviderTimeout,
    ProviderUnavailable,
    ProviderUsage,
    ProviderValidationError,
)

# Q3: models whose response supports a compact URL ref (no base64 blob, no
# storage layer). ``gpt-image-1`` is base64-only and waits for α8.4 storage.
_SUPPORTED_MODELS = frozenset({"dall-e-3", "dall-e-2"})
_DEFAULT_MODEL = "dall-e-3"

_GENERATIONS_PATH = "/images/generations"


class OpenAIImageProvider:
    """Synchronous OpenAI image-generation adapter (``Capability.IMAGE``)."""

    metadata = ProviderMetadata(
        id="openai-image",
        name="OpenAI Images",
        capability=Capability.IMAGE,
        supports_polling=False,
        supports_webhooks=False,
        version="1.0",
    )

    def __init__(self, *, client: httpx.AsyncClient) -> None:
        """Take a fully-configured, pre-authenticated shared client (Q9, W8.1.1).

        The DI layer builds the client with ``base_url``, the ``Authorization``
        header, and the per-attempt ``timeout`` already set. The adapter holds no
        configuration and no secret of its own.
        """
        self._client = client

    async def generate_image(self, req: GenerateImageRequest) -> GenerateImageResponse:
        model = req.model or _DEFAULT_MODEL
        if model not in _SUPPORTED_MODELS:
            # A bad model is a malformed request, not a provider fault — terminal.
            raise ProviderValidationError(
                f"unsupported image model {model!r} " f"(supported: {sorted(_SUPPORTED_MODELS)})"
            )

        payload: dict[str, Any] = {
            "model": model,
            "prompt": req.prompt,
            "response_format": "url",  # Q3: compact URL ref, no storage required
            "n": 1,
        }
        if req.size:
            payload["size"] = req.size

        response = await self._post(payload)
        self._raise_for_status(response)
        data = self._parse_data(response)

        image_ref = str(data[0]["url"])
        # W8.1.3: identical shape to MockImageProvider — SUCCEEDED, image_ref set,
        # output={"image_ref", "size"}, usage.unit="images". Only values differ.
        return GenerateImageResponse(
            request_id=req.request_id,
            provider=self.metadata.id,
            status=ProviderStatus.SUCCEEDED,
            output={"image_ref": image_ref, "size": req.size},
            usage=ProviderUsage(unit="images", quantity=len(data)),
            image_ref=image_ref,
        )

    async def health(self) -> ProviderHealth:
        # Q10: static — the registry does not consult health yet, so a live probe
        # would add network + auth surface for no behavioural gain.
        return ProviderHealth(healthy=True, detail="static")

    # -- one HTTP request (W7.6.2) ------------------------------------------ #

    async def _post(self, payload: dict[str, Any]) -> httpx.Response:
        """Perform exactly one request; map transport failures to transient errors."""
        try:
            return await self._client.post(_GENERATIONS_PATH, json=payload)
        except httpx.TimeoutException as exc:  # transient — runner retries
            raise ProviderTimeout(f"openai image request timed out: {exc}") from exc
        except httpx.HTTPError as exc:  # transient — connection / transport fault
            raise ProviderUnavailable(f"openai image request failed: {exc}") from exc

    # -- status → neutral error (Q7) ---------------------------------------- #

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        status = response.status_code
        if status < 400:
            return
        message = OpenAIImageProvider._error_message(response)
        if status in (401, 403):
            raise ProviderAuthenticationError(message)  # terminal
        if status == 429:
            raise ProviderRateLimited(message)  # transient
        if status >= 500:
            raise ProviderUnavailable(message)  # transient
        # Any other 4xx — bad request, content-policy rejection, unsupported
        # parameter — is terminal: a retry cannot fix it.
        raise ProviderValidationError(message)

    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        detail = OpenAIImageProvider._json_or_none(response)
        if isinstance(detail, dict):
            err = detail.get("error")
            if isinstance(err, dict):
                text = err.get("message")
                if isinstance(text, str) and text:
                    return f"openai image error {response.status_code}: {text}"
        return f"openai image error {response.status_code}"

    # -- 200 body parsing --------------------------------------------------- #

    @staticmethod
    def _parse_data(response: httpx.Response) -> list[dict[str, Any]]:
        body = OpenAIImageProvider._json_or_none(response)
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list) or not data:
            # A 200 with no image is a provider anomaly; transient so the runner
            # may re-dispatch (bounded by the workflow definition's retry limit).
            raise ProviderUnavailable("openai image response missing a non-empty 'data' array")
        first = data[0]
        if not isinstance(first, dict) or not first.get("url"):
            raise ProviderUnavailable("openai image response item missing 'url'")
        return data

    @staticmethod
    def _json_or_none(response: httpx.Response) -> Any | None:
        try:
            return response.json()
        except ValueError:
            return None
