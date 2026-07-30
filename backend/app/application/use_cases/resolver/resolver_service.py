"""α8.5e.5 — resolver service (Decision-plane composition).

The single entry point the Execution plane calls: load one immutable catalogue snapshot
(W8.5e.6) + the current runtime snapshot + the deployment's executable adapter set, then
run the pure resolver to get an ordered, explainable candidate list. It performs no writes
(W8.5e.2/.3) and no provider execution (W8.5e.1) — execution decides whether to stop after
the first success or walk the list.

It takes the executable set for the same reason ``ResolverCapabilityResolver`` does: under
ADR-0054 DISP-1 a resolution is well-formed only against a declared set, so a second
Decision-plane entry point that resolved without one could recommend an adapter this
deployment cannot construct.
"""

from __future__ import annotations

from app.application.interfaces.catalogue_reader import ICatalogueReader
from app.application.interfaces.image_generator import IImageAdapterRegistry
from app.application.interfaces.runtime_state_reader import IRuntimeStateReader
from app.domain.resolver import (
    RESOLVER_VERSION,
    ExecutableAdapters,
    Resolution,
    ResolveRequest,
    resolve as resolve_candidates,
)


class CatalogueNotSeededError(RuntimeError):
    """Raised when resolution is attempted before the catalogue has been seeded."""


class ResolverService:
    """Compose the catalogue + runtime readers with the pure resolver."""

    def __init__(
        self,
        catalogue_reader: ICatalogueReader,
        runtime_reader: IRuntimeStateReader,
        adapter_registry: IImageAdapterRegistry,
    ) -> None:
        self._catalogue = catalogue_reader
        self._runtime = runtime_reader
        self._adapters = adapter_registry

    async def resolve(
        self, request: ResolveRequest, *, resolver_version: str = RESOLVER_VERSION
    ) -> Resolution:
        catalogue = await self._catalogue.load_snapshot()
        if catalogue is None:
            raise CatalogueNotSeededError(
                "provider catalogue is not seeded; run scripts/seed_providers.py"
            )
        runtime = await self._runtime.load_snapshot()
        executable = ExecutableAdapters(adapter_ids=self._adapters.supported_adapters())
        return resolve_candidates(
            request, catalogue, runtime, executable, resolver_version=resolver_version
        )
