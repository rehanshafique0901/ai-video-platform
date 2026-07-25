"""α8.6 Increment 4 — DB-backed model manager over the ``model_cache`` registry.

Implements :class:`IModelManager` against the persistent registry. Increment 4 has
no downloader (no local adapter until Increment 6): ``ensure_available`` returns a
handle for a model already registered as ``ready`` and raises
:class:`ModelUnavailableError` otherwise. It never fetches weights.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.interfaces.model_manager import (
    IModelManager,
    LocalModel,
    ModelUnavailableError,
)
from app.infrastructure.repositories.model_cache_repository import ModelCacheRepository

_STATUS_READY = "ready"


class ModelCacheManager(IModelManager):
    """Resolve local models from the persistent ``model_cache`` registry."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def ensure_available(self, model_ref: str) -> LocalModel:
        async with self._session_factory() as session:
            repo = ModelCacheRepository(session)
            cached = await repo.get(model_ref)
            if cached is None or cached.status != _STATUS_READY or not cached.local_path:
                raise ModelUnavailableError(
                    f"model {model_ref!r} is not available in the local cache "
                    "(no downloader until Increment 6)"
                )
            await repo.touch_last_used(model_ref)
            await session.commit()
            return LocalModel(
                model_ref=cached.model_ref,
                local_path=cached.local_path,
                revision=cached.version,
                from_cache=True,
            )
