"""α8.5e.5 — resolver service (Decision-plane composition).

The single entry point the Execution plane calls: load one immutable catalogue snapshot
(W8.5e.6) + the current runtime snapshot, then run the pure resolver to get an ordered,
explainable candidate list. It performs no writes (W8.5e.2/.3) and no provider execution
(W8.5e.1) — execution decides whether to stop after the first success or walk the list.
"""

from __future__ import annotations

from app.application.interfaces.catalogue_reader import ICatalogueReader
from app.application.interfaces.runtime_state_reader import IRuntimeStateReader
from app.domain.resolver import (
    RESOLVER_VERSION,
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
    ) -> None:
        self._catalogue = catalogue_reader
        self._runtime = runtime_reader

    async def resolve(
        self, request: ResolveRequest, *, resolver_version: str = RESOLVER_VERSION
    ) -> Resolution:
        catalogue = await self._catalogue.load_snapshot()
        if catalogue is None:
            raise CatalogueNotSeededError(
                "provider catalogue is not seeded; run scripts/seed_providers.py"
            )
        runtime = await self._runtime.load_snapshot()
        return resolve_candidates(request, catalogue, runtime, resolver_version=resolver_version)
