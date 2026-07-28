"""Port: AI publish-metadata generator — owned by the Publishing application layer (α9.1).

This is the **publishing-owned** seam behind which an AI adapter *suggests* publish metadata
(title / description / hashtags) for a finished video. Per ADR-0049:

* The port + its DTOs are **neutral** — they carry only structured metadata and plain caps, and
  deliberately reference **no** ``ContentPackage`` or any ``app.domain.publishing`` type — so an AI
  adapter can implement this without creating any dependency on the Publishing bounded context. The
  dependency direction is strictly one-way (Publishing owns the abstraction; the AI subsystem
  supplies the adapter; AI never imports Publishing).
* The capability is **advisory only** (ADR-0049 Invariant 1): the caller (the
  ``GeneratePublishMetadata`` use case) always retains a deterministic template fallback and treats
  any :class:`PublishMetadataGenerationError` as "degrade to the deterministic path".
* **Prompt construction lives entirely inside the adapter** (Invariant / requirement): Publishing
  supplies only the structured :class:`PublishMetadataRequest`; no provider-specific prompt text
  ever lives in the Publishing layer.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class PublishMetadataGenerationError(RuntimeError):
    """Raised by an adapter when it cannot produce valid metadata.

    Covers every AI failure mode (provider unavailable/rate-limited/timeout/auth/validation, a
    non-terminal or empty response, or output that cannot be normalised within the caps). The
    Publishing use case catches this and falls back to the deterministic template (ADR-0049
    Invariant 3) — it is never surfaced as a publish-blocking error.
    """


@dataclass(frozen=True, slots=True)
class PublishMetadataRequest:
    """Neutral, structured input for one metadata suggestion.

    ``context`` is a plain, single-line description of the video (assembled by Publishing from the
    project it owns). The ``max_*`` caps are the **strictest** destination limits the suggestion
    must fit within, so a generated caption can never become a permanent publish failure; the
    destination adapter remains the sole boundary enforcer (defence in depth). ``request_id`` is a
    caller-minted id for tracing / future usage dedup.
    """

    request_id: str
    context: str
    max_title_chars: int
    max_description_chars: int
    max_tags_total_chars: int
    max_tag_count: int
    locale: str | None = None


@dataclass(frozen=True, slots=True)
class MetadataProvenance:
    """How a suggestion was produced. **Ephemeral** — response-only, never persisted (Invariant 5).

    ``generator`` is ``"llm"`` for an AI suggestion or ``"template"`` for the deterministic
    fallback; ``is_fallback`` makes the deterministic path explicit in the response contract
    (avoid implicit behaviour). ``model`` / ``prompt_template_version`` are populated only for the
    LLM path and are never stored.
    """

    generator: str
    is_fallback: bool
    model: str | None = None
    prompt_template_version: str | None = None


@dataclass(frozen=True, slots=True)
class GeneratedPublishMetadata:
    """A suggested (or deterministically-fallen-back) set of publish metadata."""

    title: str
    description: str
    tags: tuple[str, ...]
    provenance: MetadataProvenance


class IPublishMetadataGenerator(ABC):
    @abstractmethod
    async def generate(self, req: PublishMetadataRequest) -> GeneratedPublishMetadata:
        """Produce suggested metadata within the request's caps, or raise
        :class:`PublishMetadataGenerationError`.

        Implementations MUST NOT import ``app.domain.publishing`` (or any Publishing module):
        the request/response DTOs are the entire contract. Implementations MUST be
        cancellation-safe — if the awaiting task is cancelled, the underlying provider call is
        cancelled cleanly and ``asyncio.CancelledError`` propagates (never swallowed, no leaked
        background task).
        """
        ...
