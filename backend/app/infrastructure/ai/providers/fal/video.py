"""Fal.ai video provider — the first real *async* capability adapter (Slice α8.2).

An **asynchronous** adapter over the Fal.ai queue *submit* endpoint that
implements the :class:`~app.infrastructure.ai.providers.ports.VideoProvider`
protocol. It is a strict leaf module: it imports only ``httpx`` and the neutral
provider DTOs/errors — never the runner, dispatcher, recorder, workflow domain,
or any config/secret source.

Unlike the synchronous OpenAI image adapter (α8.1), a video job does **not**
complete inline. The adapter makes **exactly one** HTTP call — it *submits* the
job to the Fal queue and immediately returns ``IN_PROGRESS`` + a
``provider_job_id`` (the Fal ``request_id``). The α7.2 runner then pauses the
run, checkpoints the job id + the opaque ``output`` envelope, and emits
``WorkflowRunPaused`` (all existing behaviour — α8.2 writes no runner code). The
adapter **never** polls, waits, or resolves the job: completion (poll / webhook
/ resume / terminal usage) is α8.3.

Signed-off invariants this module upholds (see
``docs/engineering/PHASE3_ALPHA8_2_PREFLIGHT.md``):

* **W8.1.1 — configuration-blind.** The adapter receives a fully-configured,
  pre-authenticated ``httpx.AsyncClient`` (base URL, ``Authorization: Key …``
  header, timeout all baked in by the DI layer). It performs **no** env / DB /
  filesystem / vault lookup and never sees the raw API key (constructors receive
  a client that already carries the secret; they never retrieve it).
* **W8.2.1 — observational equivalence.** Returns the *same* DTO
  (:class:`GenerateVideoResponse`) with the *same* ``IN_PROGRESS`` status, a set
  ``provider_job_id``, and an ``output`` envelope as ``MockVideoProvider``; the
  runner cannot tell which produced the response — only the values (real Fal
  ``request_id`` + URLs, provider id) differ.
* **W8.2.2 — stop at the pause boundary.** The adapter only ever returns
  ``IN_PROGRESS``; it never drives the run to a terminal state.
* **W8.2.3 — never mutates orchestration state.** No resume, complete,
  checkpoint, event, or usage write — a pure request→response leaf with no
  reference to the UoW, event bus, checkpoint store, or usage recorder.
* **W7.6.2 — one dispatch, no internal retry.** Exactly one HTTP request per
  call; every transient failure is raised as a transient :class:`ProviderError`
  so the *runner* re-dispatches under the same deterministic ``request_id``.

Error mapping (same map as α8.1), HTTP status → neutral error, nothing HTTP
leaks upward:

===========================  =============================  =========
Condition                    Error                          Class
===========================  =============================  =========
401 / 403                    ``ProviderAuthenticationError``  terminal
400 / 422 / other 4xx        ``ProviderValidationError``      terminal
429                          ``ProviderRateLimited``          transient
5xx / connection             ``ProviderUnavailable``          transient
timeout                      ``ProviderTimeout``              transient
2xx (accepted / queued)      ``GenerateVideoResponse``        —
===========================  =============================  =========
"""

from __future__ import annotations

from typing import Any

import httpx

from app.application.interfaces.providers import (
    Capability,
    GenerateVideoRequest,
    GenerateVideoResponse,
    ProviderAuthenticationError,
    ProviderHealth,
    ProviderMetadata,
    ProviderRateLimited,
    ProviderStatus,
    ProviderTimeout,
    ProviderUnavailable,
    ProviderValidationError,
)

# The opaque checkpoint envelope's schema version (Q4). α8.3 reads the persisted
# payload; bumping this lets the completion contract evolve without breaking
# older checkpoints.
_ENVELOPE_SCHEMA_VERSION = 1

# Q7: supported Fal video routes. The workflow ``model`` arg carries the route;
# an unknown route is a malformed request (terminal), not a provider fault.
_SUPPORTED_MODELS = frozenset(
    {
        "fal-ai/ltx-video",
        "fal-ai/kling-video/v1/standard/text-to-video",
        "fal-ai/minimax-video",
    }
)
_DEFAULT_MODEL = "fal-ai/ltx-video"


class FalVideoProvider:
    """Asynchronous Fal.ai video-generation adapter (``Capability.VIDEO``).

    Submit-only (α8.2): one POST to the Fal queue, returns ``IN_PROGRESS`` + the
    Fal ``request_id`` as ``provider_job_id``. Completion is α8.3.
    """

    metadata = ProviderMetadata(
        id="fal-video",
        name="Fal.ai Video",
        capability=Capability.VIDEO,
        # Q9: truthful for Fal — the α8.3 completion service branches on these
        # metadata flags (not provider identity) to pick a completion strategy.
        supports_polling=True,
        supports_webhooks=True,
        version="1.0",
    )

    def __init__(self, *, client: httpx.AsyncClient) -> None:
        """Take a fully-configured, pre-authenticated shared client (W8.1.1).

        The DI layer builds the client with ``base_url``, the
        ``Authorization: Key …`` header, and the per-attempt ``timeout`` already
        set. The adapter holds no configuration and no secret of its own.
        """
        self._client = client

    async def generate_video(self, req: GenerateVideoRequest) -> GenerateVideoResponse:
        model = req.model or _DEFAULT_MODEL
        if model not in _SUPPORTED_MODELS:
            # A bad route is a malformed request, not a provider fault — terminal,
            # and raised before any HTTP call.
            raise ProviderValidationError(
                f"unsupported video model {model!r} " f"(supported: {sorted(_SUPPORTED_MODELS)})"
            )

        payload: dict[str, Any] = {"prompt": req.prompt}
        if req.duration_seconds:
            payload["duration"] = req.duration_seconds

        response = await self._submit(model, payload)
        self._raise_for_status(response)
        data = self._parse_submit(response)

        job_id = str(data["request_id"])
        # W8.2.1: identical shape to MockVideoProvider — IN_PROGRESS, provider_job_id
        # set, opaque ``output`` envelope, no usage. Only the values differ.
        # W8.2.2/W8.2.3: we return IN_PROGRESS and nothing else — the runner owns
        # every state transition from here.
        return GenerateVideoResponse(
            request_id=req.request_id,
            provider=self.metadata.id,
            status=ProviderStatus.IN_PROGRESS,
            provider_job_id=job_id,
            output={
                # Q4: versioned opaque envelope — the runner checkpoints it
                # verbatim (W7.6.1) and α8.3 reads the completion coordinates.
                "schema_version": _ENVELOPE_SCHEMA_VERSION,
                "provider": "fal",
                "provider_job_id": job_id,
                "status_url": data.get("status_url"),
                "response_url": data.get("response_url"),
            },
            usage=None,  # Q5: no billable outcome on submit; α8.3 records terminal usage
        )

    async def health(self) -> ProviderHealth:
        # Static — the registry does not consult health, so a live probe would add
        # network + auth surface for no behavioural gain (mirrors α8.1).
        return ProviderHealth(healthy=True, detail="static")

    # -- one HTTP request (W7.6.2) ------------------------------------------ #

    async def _submit(self, model: str, payload: dict[str, Any]) -> httpx.Response:
        """Perform exactly one submit request; map transport failures to transient errors."""
        try:
            return await self._client.post(f"/{model}", json=payload)
        except httpx.TimeoutException as exc:  # transient — runner retries
            raise ProviderTimeout(f"fal video submit timed out: {exc}") from exc
        except httpx.HTTPError as exc:  # transient — connection / transport fault
            raise ProviderUnavailable(f"fal video submit failed: {exc}") from exc

    # -- status → neutral error (same map as α8.1) -------------------------- #

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        status = response.status_code
        if status < 400:
            return
        message = FalVideoProvider._error_message(response)
        if status in (401, 403):
            raise ProviderAuthenticationError(message)  # terminal
        if status == 429:
            raise ProviderRateLimited(message)  # transient
        if status >= 500:
            raise ProviderUnavailable(message)  # transient
        # Any other 4xx (400 / 422 / unsupported input) is terminal: a retry
        # cannot fix a malformed request.
        raise ProviderValidationError(message)

    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        detail = FalVideoProvider._json_or_none(response)
        if isinstance(detail, dict):
            # Fal surfaces errors under 'detail' (str or list) or 'error'.
            for key in ("detail", "error", "message"):
                text = detail.get(key)
                if isinstance(text, str) and text:
                    return f"fal video error {response.status_code}: {text}"
        return f"fal video error {response.status_code}"

    # -- submit body parsing ------------------------------------------------ #

    @staticmethod
    def _parse_submit(response: httpx.Response) -> dict[str, Any]:
        body = FalVideoProvider._json_or_none(response)
        if not isinstance(body, dict) or not body.get("request_id"):
            # A 2xx submit with no request_id is a provider anomaly; transient so
            # the runner may re-dispatch (bounded by the workflow retry limit).
            raise ProviderUnavailable("fal video submit response missing 'request_id'")
        return body

    @staticmethod
    def _json_or_none(response: httpx.Response) -> Any | None:
        try:
            return response.json()
        except ValueError:
            return None
