"""Port: run one generation to completion (α9.7).

The seam between the generation **worker** (which owns claiming, leasing and reaping) and the
generation **pipeline** (:class:`GenerateVideo`, which owns the actual work).

It exists because ``GenerateVideo`` needs a live database session — its capability resolver
reads the provider catalogue through one — and handing a session to an application-layer worker
would drag an infrastructure type across the seam and make the worker impossible to unit-test
without a database. Behind this port, the adapter opens a session per run and disposes of it;
in front of it, the worker sees one coroutine that either returns a result or raises.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.application.use_cases.generation.request import GenerateVideoRequest
from app.application.use_cases.generation.results import GenerateVideoResult


class IGenerationRunner(ABC):
    """Execute a single generation request end to end."""

    @abstractmethod
    async def run(self, request: GenerateVideoRequest) -> GenerateVideoResult:
        """Run the pipeline. Raises only on infrastructure failure; a *generation* failure
        comes back as a :class:`GenerateVideoResult` with ``status = FAILED``."""
        ...
