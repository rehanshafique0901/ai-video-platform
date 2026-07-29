"""α9.7 — session-scoped :class:`IGenerationRunner` adapter.

Opens one short-lived :class:`AsyncSession` per generation, builds :class:`GenerateVideo` over
it, and runs the pipeline. The session exists only so the capability resolver can read the
provider catalogue; the pipeline's own persistence goes through
:class:`SqlExecutionRuntimeStore`, which manages its own short transactions because a run is
far too long for one to span it.

``builder`` is injected rather than imported so this adapter never reaches back into the
composition root — the container passes :func:`get_generate_video_use_case` in as a callable,
keeping the dependency one-way (container → infrastructure).
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.interfaces.generation_runner import IGenerationRunner
from app.application.use_cases.generation.generate_video import GenerateVideo
from app.application.use_cases.generation.request import GenerateVideoRequest
from app.application.use_cases.generation.results import GenerateVideoResult


class SessionScopedGenerationRunner(IGenerationRunner):
    """Run one generation inside its own database session."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        builder: Callable[[AsyncSession], GenerateVideo],
    ) -> None:
        self._session_factory = session_factory
        self._builder = builder

    async def run(self, request: GenerateVideoRequest) -> GenerateVideoResult:
        async with self._session_factory() as session:
            return await self._builder(session).execute(request)


__all__ = ["SessionScopedGenerationRunner"]
