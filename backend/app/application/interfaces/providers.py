"""The provider runtime's neutral DTOs, enums, metadata, and errors (Slice α7.4).

This module is the **application-layer boundary** for the provider runtime
(ADR-0041 D1–D4). It contains only *neutral* types — no SDK imports, no HTTP, no
infrastructure, and (deliberately) **no workflow domain import** — so the
provider capability leaf (``app/infrastructure/ai/providers/``) can implement
against it while staying a strict ``import-linter`` leaf.

The runner-facing **port** (``ProviderDispatcherPort``) lives in the sibling
module :mod:`app.application.interfaces.provider_dispatcher` because it references
``StepCommand`` (a workflow-domain type); keeping it separate is what lets the
capability leaf depend on these DTOs without transitively importing the workflow
domain. Everything provider-specific — the capability ``Protocol``s, the registry,
and the mock adapters — lives below the infrastructure boundary; the concrete
``StepCommandDispatcher`` sits just above that leaf (``app/infrastructure/ai/
dispatcher.py``).

α7.4 is **mock-only and almost entirely architecture**: there is no HTTP client,
no external API, no broker, no retries, no fallback, no usage accounting, and no
event publishing here. Those arrive in later slices behind these same types.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Capability(StrEnum):
    """The four provider capability kinds (ADR-0041 D1), keyed by ``plugin_kind``."""

    LLM = "llm"
    IMAGE = "image"
    VIDEO = "video"
    VOICE = "voice"


class ProviderStatus(StrEnum):
    """The outcome of a capability call.

    ``SUCCEEDED`` / ``FAILED`` are inline terminal results; ``IN_PROGRESS`` marks
    an async job (e.g. video) that a later completion service (α8.3) resolves via
    the returned ``provider_job_id``. This single shape lets a mock be fully
    deterministic while still exercising the async path.
    """

    SUCCEEDED = "succeeded"
    IN_PROGRESS = "in_progress"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    """Units consumed by one capability call — the seam the α7.5 Usage Recorder prices.

    ``unit`` names what is counted (``"tokens"`` / ``"images"`` / ``"seconds"`` /
    ``"characters"``); ``quantity`` is how many. Defined here, **consumed in α7.5**
    — α7.4 only populates it deterministically from the mock request.
    """

    unit: str
    quantity: int
    detail: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    """A provider's self-reported health, consulted before selection (ADR-0041 D3)."""

    healthy: bool
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderMetadata:
    """Immutable identity/capability descriptor every provider exposes (α7.4 addition).

    Mocks expose it now; real providers (OpenAI / Gemini / Runway / Fal / Ideogram /
    Leonardo, …) expose it identically later. ``supports_polling`` /
    ``supports_webhooks`` describe how an async job is completed (α8.3), so the
    completion layer can branch on metadata rather than provider identity.
    """

    id: str
    name: str
    capability: Capability
    supports_polling: bool
    supports_webhooks: bool
    version: str


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    """The common envelope every capability response carries.

    ``request_id`` is the client-minted id that dedupes usage + completion
    (ADR-0041 D1/D13); ``provider`` is the registry key that served the call;
    ``provider_job_id`` is set **iff** ``status is IN_PROGRESS`` (async → completion,
    α8.3); ``usage`` feeds the α7.5 recorder; ``error`` carries a terminal message
    on ``FAILED``. ``output`` is the capability-neutral payload bag.
    """

    request_id: str
    provider: str
    status: ProviderStatus
    output: Mapping[str, Any] = field(default_factory=dict)
    provider_job_id: str | None = None
    usage: ProviderUsage | None = None
    error: str | None = None


# --------------------------------------------------------------------------- #
# Per-capability request / response pairs (Q2 — immutable dataclasses).
# Interfaces (the capability Protocols) live in the infrastructure leaf; these
# neutral DTOs live here so both layers and the tests can construct them.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class GenerateTextRequest:
    request_id: str
    prompt: str
    model: str | None = None
    max_tokens: int | None = None
    params: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GenerateTextResponse(ProviderResponse):
    text: str = ""


@dataclass(frozen=True, slots=True)
class GenerateImageRequest:
    request_id: str
    prompt: str
    model: str | None = None
    size: str | None = None
    params: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GenerateImageResponse(ProviderResponse):
    image_ref: str | None = None


@dataclass(frozen=True, slots=True)
class GenerateVideoRequest:
    request_id: str
    prompt: str
    model: str | None = None
    duration_seconds: int | None = None
    params: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GenerateVideoResponse(ProviderResponse):
    video_ref: str | None = None


@dataclass(frozen=True, slots=True)
class GenerateSpeechRequest:
    request_id: str
    text: str
    voice: str | None = None
    model: str | None = None
    params: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GenerateSpeechResponse(ProviderResponse):
    audio_ref: str | None = None


# --------------------------------------------------------------------------- #
# Typed provider errors (Q7) — pure domain/application semantics, NO HTTP mapping.
# ``transient`` classifies for the future retry/fallback policy (ADR-0041 D10);
# α7.4 defines and classifies them but schedules no retries.
# --------------------------------------------------------------------------- #


class ProviderError(Exception):
    """Base of the provider error hierarchy. ``transient`` is retry classification."""

    transient: bool = False


class ProviderUnavailable(ProviderError):  # noqa: N818 — signed-off / ADR-0041 canonical name
    """The provider (or its upstream) is temporarily down — transient."""

    transient = True


class ProviderRateLimited(ProviderError):  # noqa: N818 — signed-off / ADR-0041 canonical name
    """The provider throttled the request — transient."""

    transient = True


class ProviderTimeout(ProviderError):  # noqa: N818 — signed-off / ADR-0041 canonical name
    """The call exceeded its deadline — transient."""

    transient = True


class ProviderAuthenticationError(ProviderError):
    """Credentials were rejected — terminal (a retry cannot fix it)."""


class ProviderValidationError(ProviderError):
    """The request was malformed/unacceptable — terminal."""


class NoProviderAvailable(ProviderError):  # noqa: N818 — plays ADR-0041's NoHealthyProvider role
    """No provider is registered/configured for the requested capability.

    Raised by the registry's ``resolve`` (it plays ADR-0041's ``NoHealthyProvider``
    role; renamed because health-ordering / fallback are deferred until multiple
    real providers exist, per the α7.4 sign-off Q4). Terminal for the calling step.
    """
