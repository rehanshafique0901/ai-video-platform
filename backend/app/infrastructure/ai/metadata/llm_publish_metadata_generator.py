"""LLM-backed publish-metadata generator (α9.1, ADR-0049).

Implements the Publishing-owned :class:`IPublishMetadataGenerator` port using the existing
``Capability.LLM`` seam (ADR-0041). It is the sole publishing→AI bridge: it depends only on the AI
capability + the neutral port DTOs and **never** imports the Publishing bounded context.

Design points (ADR-0049 + α9.1 requirements):

* **Prompt isolation** — all prompt text lives here (``_INSTRUCTION`` / :func:`_render_prompt`);
  Publishing supplies only the structured :class:`PublishMetadataRequest.context`.
* **Provider independence** — the adapter resolves whatever provider serves ``Capability.LLM`` from
  the registry (the deterministic ``MockLLMProvider`` by default); callers never know which.
* **Cancellation safety** — the provider call is wrapped in ``asyncio.wait_for``; on cancellation
  the inner call is cancelled and ``CancelledError`` propagates (never swallowed → no leaked task).
* **Graceful degradation** — any provider error, timeout, non-terminal/empty response, or
  unusable output is mapped to :class:`PublishMetadataGenerationError` for the use case to fall
  back on. Logs carry only a coarse ``reason`` — never prompts, generated text, tokens, or
  provider responses (α9.1 logging requirement).
"""

from __future__ import annotations

import asyncio
import re
from typing import cast

import structlog

from app.application.interfaces.providers import (
    Capability,
    GenerateTextRequest,
    ProviderError,
    ProviderStatus,
)
from app.application.interfaces.publish_metadata_generator import (
    GeneratedPublishMetadata,
    IPublishMetadataGenerator,
    MetadataProvenance,
    PublishMetadataGenerationError,
    PublishMetadataRequest,
)
from app.infrastructure.ai.providers.ports import LLMProvider
from app.infrastructure.ai.providers.registry import ProviderRegistry

_LOGGER = structlog.get_logger(__name__)

# The prompt-template version — provenance only (never persisted). Bump when the prompt/parse
# contract below changes so a future real provider's output is attributable.
PROMPT_TEMPLATE_VERSION = "cap-hashtag/v1"

# All provider-facing prompt text is isolated here (prompt isolation). The video ``context`` is
# placed FIRST so a deterministic echo provider (the mock) yields context-derived metadata.
_INSTRUCTION = (
    "Write a concise, engaging social-video title on the first line, then a short description, "
    "then relevant hashtag keywords. Keep it faithful to the video above."
)

# Generic normalisation only (NOT provider-specific): strip a single leading "[tag] " prefix if a
# provider prepends one, so the echoed body starts at the real content.
_LEADING_TAG = re.compile(r"^\[[^\]]*\]\s*")
_WORD = re.compile(r"[A-Za-z0-9]+")
_STOPWORDS = frozenset(
    {
        "this",
        "that",
        "with",
        "from",
        "your",
        "about",
        "into",
        "have",
        "will",
        "then",
        "line",
        "video",
        "title",
        "short",
        "write",
        "description",
        "hashtag",
        "hashtags",
        "keywords",
        "relevant",
        "concise",
        "engaging",
        "social",
        "keep",
        "faithful",
        "above",
    }
)


def _render_prompt(req: PublishMetadataRequest) -> str:
    """Build the LLM prompt from the neutral context (context first, instruction second)."""
    return f"{req.context.strip()}\n\n{_INSTRUCTION}"


def _truncate(text: str, limit: int) -> str:
    return text.strip()[:limit].strip()


def _keywords(text: str, *, max_count: int, max_total_chars: int) -> tuple[str, ...]:
    """Deterministically derive hashtag keywords: first-seen alphanumeric words within caps."""
    seen: list[str] = []
    for word in _WORD.findall(text.lower()):
        if len(word) < 4 or word in _STOPWORDS or word in seen:
            continue
        seen.append(word)
    tags: list[str] = []
    total = 0
    for word in seen:
        if len(tags) >= max_count or total + len(word) > max_total_chars:
            break
        tags.append(word)
        total += len(word)
    return tuple(tags)


def _parse(text: str, req: PublishMetadataRequest) -> tuple[str, str, tuple[str, ...]]:
    """Deterministically parse a completion into (title, description, tags), enforcing caps."""
    body = _LEADING_TAG.sub("", text, count=1).strip()
    paragraph = body.split("\n\n", 1)[0].strip()
    lines = [line.strip() for line in paragraph.splitlines() if line.strip()]
    title = _truncate(lines[0] if lines else body, req.max_title_chars)
    description = _truncate(paragraph or title, req.max_description_chars)
    tags = _keywords(
        paragraph,
        max_count=req.max_tag_count,
        max_total_chars=req.max_tags_total_chars,
    )
    return title, description, tags


class LlmPublishMetadataGenerator(IPublishMetadataGenerator):
    """Suggest publish metadata via the ``Capability.LLM`` provider resolved from the registry."""

    def __init__(
        self,
        registry: ProviderRegistry,
        *,
        timeout_seconds: float,
        model: str | None = None,
    ) -> None:
        self._registry = registry
        self._timeout = timeout_seconds
        self._model = model

    async def generate(self, req: PublishMetadataRequest) -> GeneratedPublishMetadata:
        prompt = _render_prompt(req)
        try:
            provider = cast(LLMProvider, self._registry.resolve(Capability.LLM))
            resp = await asyncio.wait_for(
                provider.generate_text(
                    GenerateTextRequest(
                        request_id=req.request_id,
                        prompt=prompt,
                        model=self._model,
                    )
                ),
                timeout=self._timeout,
            )
        except (ProviderError, TimeoutError) as exc:
            # ProviderError covers unavailable/rate-limited/timeout/auth/validation/no-provider;
            # TimeoutError is raised by asyncio.wait_for. CancelledError is NOT caught here
            # (it inherits from BaseException) so cancellation propagates cleanly.
            _LOGGER.warning("publish_metadata.generation_failed", reason=type(exc).__name__)
            raise PublishMetadataGenerationError("llm metadata generation failed") from exc

        if resp.status is not ProviderStatus.SUCCEEDED or not resp.text.strip():
            _LOGGER.warning("publish_metadata.generation_failed", reason="non_terminal_or_empty")
            raise PublishMetadataGenerationError("llm returned no usable text")

        title, description, tags = _parse(resp.text, req)
        if not title:
            _LOGGER.warning("publish_metadata.generation_failed", reason="empty_title")
            raise PublishMetadataGenerationError("llm produced an empty title")

        return GeneratedPublishMetadata(
            title=title,
            description=description,
            tags=tags,
            provenance=MetadataProvenance(
                generator="llm",
                is_fallback=False,
                model=self._model,
                prompt_template_version=PROMPT_TEMPLATE_VERSION,
            ),
        )
