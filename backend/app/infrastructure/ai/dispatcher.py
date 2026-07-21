"""StepCommand dispatcher — the imperative interpreter of commands (Slice α7.4).

Maps a declarative :class:`StepCommand` ``{kind, args}`` to a resolved provider
capability call via an **explicit, closed table** — the only place ``kind`` strings
become provider calls (ADR-0041 D4). α7.4 covers the four provider capabilities
only; ``start_render`` and any render/export/storage/orchestration kinds are
**excluded** (Q6) and raise :class:`ProviderValidationError`.

This bridge lives one level **above** the ``app.infrastructure.ai.providers`` leaf
(it depends on both the workflow domain's ``StepCommand`` and the provider leaf),
so the leaf itself stays orchestration-free. The α7.2 runner is **not** wired to it
in this slice (D3.3) — it keeps ignoring ``StepResult.commands`` until α7.6.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast

from app.application.interfaces.provider_dispatcher import ProviderDispatcherPort
from app.application.interfaces.providers import (
    Capability,
    GenerateImageRequest,
    GenerateSpeechRequest,
    GenerateTextRequest,
    GenerateVideoRequest,
    ProviderResponse,
    ProviderValidationError,
)
from app.domain.workflow.registry import StepCommand
from app.infrastructure.ai.providers.ports import (
    ImageProvider,
    LLMProvider,
    VideoProvider,
    VoiceProvider,
)
from app.infrastructure.ai.providers.registry import PROVIDER_REGISTRY, ProviderRegistry


class StepCommandDispatcher(ProviderDispatcherPort):
    """Interpret provider-capability ``StepCommand``s against a :class:`ProviderRegistry`."""

    def __init__(self, registry: ProviderRegistry | None = None) -> None:
        self._registry = registry or PROVIDER_REGISTRY
        self._handlers: dict[str, Callable[[StepCommand], Awaitable[ProviderResponse]]] = {
            "generate_text": self._generate_text,
            "generate_image": self._generate_image,
            "generate_video": self._generate_video,
            "synthesize_voice": self._synthesize_voice,
        }

    async def dispatch(self, command: StepCommand) -> ProviderResponse:
        handler = self._handlers.get(command.kind)
        if handler is None:
            raise ProviderValidationError(
                f"command kind {command.kind!r} is not a dispatchable provider capability "
                f"(supported: {sorted(self._handlers)})"
            )
        return await handler(command)

    async def resolve_job(
        self,
        capability: Capability,
        *,
        provider_job_id: str,
        envelope: Mapping[str, Any],
    ) -> ProviderResponse:
        # α8.3 completion path: only the async VIDEO capability exposes ``resolve``.
        # A synchronous capability has no job to resolve — malformed, terminal.
        if capability is not Capability.VIDEO:
            raise ProviderValidationError(
                f"capability {capability!r} is synchronous and has no resolvable job"
            )
        provider = cast(VideoProvider, self._registry.resolve(Capability.VIDEO))
        return await provider.resolve(provider_job_id=provider_job_id, envelope=envelope)

    # -- discovery (delegated to the registry) ------------------------------ #

    def supports(self, capability: Capability) -> bool:
        return self._registry.supports(capability)

    def list_capabilities(self) -> list[Capability]:
        return self._registry.list_capabilities()

    # -- closed mapping table ----------------------------------------------- #

    async def _generate_text(self, command: StepCommand) -> ProviderResponse:
        provider = cast(LLMProvider, self._registry.resolve(Capability.LLM))
        args = command.args
        return await provider.generate_text(
            GenerateTextRequest(
                request_id=self._request_id(command),
                prompt=str(args.get("prompt", "")),
                model=args.get("model"),
                max_tokens=args.get("max_tokens"),
                params=args.get("params", {}),
            )
        )

    async def _generate_image(self, command: StepCommand) -> ProviderResponse:
        provider = cast(ImageProvider, self._registry.resolve(Capability.IMAGE))
        args = command.args
        return await provider.generate_image(
            GenerateImageRequest(
                request_id=self._request_id(command),
                prompt=str(args.get("prompt", "")),
                model=args.get("model"),
                size=args.get("size"),
                params=args.get("params", {}),
            )
        )

    async def _generate_video(self, command: StepCommand) -> ProviderResponse:
        provider = cast(VideoProvider, self._registry.resolve(Capability.VIDEO))
        args = command.args
        # α8.3: the async lifecycle's *submit* half (renamed from ``generate_video``);
        # the closed ``kind`` table above is unchanged — ``generate_video`` is the
        # workflow verb, ``submit`` the provider verb.
        return await provider.submit(
            GenerateVideoRequest(
                request_id=self._request_id(command),
                prompt=str(args.get("prompt", "")),
                model=args.get("model"),
                duration_seconds=args.get("duration_seconds"),
                params=args.get("params", {}),
            )
        )

    async def _synthesize_voice(self, command: StepCommand) -> ProviderResponse:
        provider = cast(VoiceProvider, self._registry.resolve(Capability.VOICE))
        args = command.args
        return await provider.synthesize_voice(
            GenerateSpeechRequest(
                request_id=self._request_id(command),
                text=str(args.get("text", "")),
                voice=args.get("voice"),
                model=args.get("model"),
                params=args.get("params", {}),
            )
        )

    @staticmethod
    def _request_id(command: StepCommand) -> str:
        """Extract the client-minted ``request_id`` that dedupes usage + completion.

        Required (ADR-0041 D1/D13): a missing id is a malformed command, not a
        provider fault, so it is a terminal :class:`ProviderValidationError`.
        """
        request_id = command.args.get("request_id")
        if not request_id:
            raise ProviderValidationError(
                f"command {command.kind!r} is missing required 'request_id' in args"
            )
        return str(request_id)
