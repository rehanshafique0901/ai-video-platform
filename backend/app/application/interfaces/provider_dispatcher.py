"""Port: the ``StepCommand`` → provider dispatcher (Slice α7.4).

Split out from :mod:`app.application.interfaces.providers` because it references
``StepCommand`` (a workflow-domain type). Keeping the port here — not in the
neutral DTO module — lets the provider capability leaf
(``app.infrastructure.ai.providers``) depend on the DTOs **without** transitively
importing the workflow domain, so the leaf stays a strict ``import-linter`` leaf.

This is the seam the α7.6 runner will depend on (exactly as α7.3's runner depends
on ``PublisherPort`` / the lock manager) — without importing infrastructure. The
concrete ``StepCommandDispatcher`` lives just above the leaf
(``app.infrastructure.ai.dispatcher``) and owns the closed ``kind`` → capability
mapping table (ADR-0041 D4); discovery is delegated to the registry so an
orchestrator can stay generic (ask ``supports(...)`` instead of hard-coding
``if image:``). The α7.2 runner is **not** wired to it in α7.4 (D3.3).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any

from app.application.interfaces.providers import Capability, ProviderResponse
from app.domain.workflow.registry import StepCommand


class ProviderDispatcherPort(ABC):
    """Interpret a declarative :class:`StepCommand` as a provider capability call."""

    @abstractmethod
    async def dispatch(self, command: StepCommand) -> ProviderResponse:
        """Route ``command`` to the resolved capability and return its response.

        Raises :class:`~app.application.interfaces.providers.NoProviderAvailable`
        if no provider serves the command's capability, and
        :class:`~app.application.interfaces.providers.ProviderValidationError` if
        ``command.kind`` is not a dispatchable provider capability (render / export
        / storage are excluded — Q6).
        """
        ...

    @abstractmethod
    async def resolve_job(
        self,
        capability: Capability,
        *,
        provider_job_id: str,
        envelope: Mapping[str, Any],
    ) -> ProviderResponse:
        """Resolve a previously-submitted async job to a terminal (or in-progress) result.

        The completion-path counterpart of :meth:`dispatch` (α8.3): given the
        capability that owns the job and the checkpointed ``provider_job_id`` +
        opaque ``envelope`` (the completion coordinates the adapter stashed at
        submit), route to the resolved provider's ``resolve(...)`` and return its
        neutral :class:`ProviderResponse`. Only async capabilities (VIDEO) support
        this; a synchronous capability raises
        :class:`~app.application.interfaces.providers.ProviderValidationError`.
        Raises :class:`~app.application.interfaces.providers.NoProviderAvailable`
        if no provider serves ``capability``.
        """
        ...

    @abstractmethod
    def supports(self, capability: Capability) -> bool:
        """Whether at least one provider is registered for ``capability``."""
        ...

    @abstractmethod
    def list_capabilities(self) -> list[Capability]:
        """The capabilities currently served by a registered provider."""
        ...
