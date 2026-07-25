"""α8.6 Increment 4 — model cache repository (raw SQL, ORM-less).

Reads/writes the persistent local-model registry (``model_cache``, migration
``0012``). Increment 4 ships the *registry* + a DB-backed ``IModelManager``, not a
downloader (no local adapter until Increment 6). Transaction scoping is the
caller's.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_GET_SQL = text("SELECT * FROM model_cache WHERE model_ref = :model_ref")

_TOUCH_SQL = text(
    "UPDATE model_cache SET last_used_at = now(), updated_at = now() WHERE model_ref = :model_ref"
)

_UPSERT_SQL = text(
    """
    INSERT INTO model_cache (
        model_ref, version, sha256, size_bytes, backend, execution_tier,
        supported_capabilities, local_path, status, downloaded_at
    ) VALUES (
        :model_ref, :version, :sha256, :size_bytes, :backend,
        CAST(:execution_tier AS execution_tier), :supported_capabilities,
        :local_path, :status, :downloaded_at
    )
    ON CONFLICT (model_ref) DO UPDATE SET
        version = EXCLUDED.version,
        sha256 = EXCLUDED.sha256,
        size_bytes = EXCLUDED.size_bytes,
        backend = EXCLUDED.backend,
        execution_tier = EXCLUDED.execution_tier,
        supported_capabilities = EXCLUDED.supported_capabilities,
        local_path = EXCLUDED.local_path,
        status = EXCLUDED.status,
        downloaded_at = EXCLUDED.downloaded_at,
        updated_at = now()
    """
)


@dataclass(frozen=True, slots=True)
class CachedModel:
    model_ref: str
    version: str | None
    backend: str | None
    execution_tier: str | None
    local_path: str | None
    status: str
    supported_capabilities: tuple[str, ...]


def cached_model_from_row(row: Mapping[str, Any]) -> CachedModel:
    caps = row.get("supported_capabilities") or []
    return CachedModel(
        model_ref=row["model_ref"],
        version=row.get("version"),
        backend=row.get("backend"),
        execution_tier=row.get("execution_tier"),
        local_path=row.get("local_path"),
        status=row["status"],
        supported_capabilities=tuple(caps),
    )


class ModelCacheRepository:
    """Raw-SQL accessor for the ``model_cache`` registry."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, model_ref: str) -> CachedModel | None:
        row = (await self._session.execute(_GET_SQL, {"model_ref": model_ref})).mappings().first()
        return cached_model_from_row(dict(row)) if row is not None else None

    async def touch_last_used(self, model_ref: str) -> None:
        await self._session.execute(_TOUCH_SQL, {"model_ref": model_ref})

    async def upsert(
        self,
        *,
        model_ref: str,
        status: str,
        version: str | None = None,
        sha256: bytes | None = None,
        size_bytes: int | None = None,
        backend: str | None = None,
        execution_tier: str | None = None,
        supported_capabilities: Sequence[str] = (),
        local_path: str | None = None,
        downloaded_at: Any = None,
    ) -> None:
        await self._session.execute(
            _UPSERT_SQL,
            {
                "model_ref": model_ref,
                "version": version,
                "sha256": sha256,
                "size_bytes": size_bytes,
                "backend": backend,
                "execution_tier": execution_tier,
                "supported_capabilities": list(supported_capabilities),
                "local_path": local_path,
                "status": status,
                "downloaded_at": downloaded_at,
            },
        )
