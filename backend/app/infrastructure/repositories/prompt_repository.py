"""SQLAlchemy implementation of ``IPromptRepository`` (Slice α6.1).

Prompts are **generation inputs** owned by a project (``PROMPT_AGGREGATE.md`` /
ADR-0036). This adapter is deliberately simple compared with the scene
repository: prompts have **no ordering key**, **no version column**, and **no
per-row optimistic-concurrency fence** — mutations are last-writer-wins and do
**not** bump ``projects.version`` (the versioned aggregate stays {project root +
scenes}).

Every method is project-scoped: reads/writes carry ``project_id`` +
``deleted_at IS NULL`` so a prompt of another project is invisible (returns
``None`` / omitted / ``False`` — anti-enumeration, inherited from α5a/α5c). The
use case established project ownership via ``IProjectRepository.get_owned``
before reaching this port.

:meth:`model_is_linkable` is the app-level gate for a client-supplied
``model_id``: ``prompts.model_id`` is ``ON DELETE SET NULL`` so the FK alone
would accept a since-retired model — the use case calls this first and maps a
``False`` to ``422`` (α6.1 Q4). ``updated_at`` is trigger-owned; the repository
never hand-sets it (there is no ``version`` to co-set, unlike the α5b/α5c CAS).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.interfaces.repositories import IPromptRepository
from app.domain.prompts.prompt import Prompt as PromptEntity
from app.infrastructure.db.models.ai_models import AIModel as AIModelRow
from app.infrastructure.db.models.scenes import Prompt as PromptRow


class PromptRepository(IPromptRepository):
    """Prompt persistence adapter. Soft-deleted rows are excluded."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---- create --------------------------------------------------------

    async def add(
        self,
        *,
        project_id: UUID,
        scene_id: UUID | None,
        kind: str,
        text_content: str,
        model_id: UUID | None,
        extra: dict[str, Any],
    ) -> PromptEntity:
        row = PromptRow(
            project_id=project_id,
            scene_id=scene_id,
            kind=kind,
            text_content=text_content,
            model_id=model_id,
            extra=extra,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return _row_to_entity(row)

    # ---- reads ---------------------------------------------------------

    async def list_owned(
        self,
        project_id: UUID,
        *,
        kind: str | None = None,
        scene_id: UUID | None = None,
    ) -> list[PromptEntity]:
        stmt = (
            select(PromptRow)
            .where(PromptRow.project_id == project_id)
            .where(PromptRow.deleted_at.is_(None))
        )
        if kind is not None:
            stmt = stmt.where(PromptRow.kind == kind)
        if scene_id is not None:
            stmt = stmt.where(PromptRow.scene_id == scene_id)
        # Total order (created_at, id) DESC → stable newest-first, no dupes/skips
        # under timestamp ties (mirrors ProjectRepository.list_owned).
        stmt = stmt.order_by(PromptRow.created_at.desc(), PromptRow.id.desc())
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_row_to_entity(r) for r in rows]

    async def get_owned(self, project_id: UUID, prompt_id: UUID) -> PromptEntity | None:
        stmt = (
            select(PromptRow)
            .where(PromptRow.id == prompt_id)
            .where(PromptRow.project_id == project_id)
            .where(PromptRow.deleted_at.is_(None))
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _row_to_entity(row) if row is not None else None

    # ---- mutations -----------------------------------------------------

    async def update_owned(
        self,
        project_id: UUID,
        prompt_id: UUID,
        changes: Mapping[str, Any],
    ) -> PromptEntity | None:
        # No version fence (ADR-0036 — prompts have no OCC column). Scoped to
        # the owning project + live rows. ``updated_at`` is trigger-owned; we
        # do not hand-set it (there is no ``version`` to co-bump).
        assert changes, "update_owned requires at least one changed column"
        upd = (
            update(PromptRow)
            .where(PromptRow.id == prompt_id)
            .where(PromptRow.project_id == project_id)
            .where(PromptRow.deleted_at.is_(None))
            .values(**changes)
            .returning(PromptRow)
        )
        row = (await self._session.execute(upd)).scalar_one_or_none()
        return _row_to_entity(row) if row is not None else None

    async def soft_delete_owned(self, project_id: UUID, prompt_id: UUID) -> bool:
        stmt = (
            update(PromptRow)
            .where(PromptRow.id == prompt_id)
            .where(PromptRow.project_id == project_id)
            .where(PromptRow.deleted_at.is_(None))
            .values(deleted_at=func.now())
            .returning(PromptRow.id)
        )
        marked = (await self._session.execute(stmt)).scalar_one_or_none()
        return marked is not None

    # ---- validation helper --------------------------------------------

    async def model_is_linkable(self, model_id: UUID) -> bool:
        # Linkable iff the ai_models row exists and is not 'retired' (α6.1 Q4).
        # ai_models is a system registry with no soft-delete.
        stmt = (
            select(AIModelRow.id)
            .where(AIModelRow.id == model_id)
            .where(AIModelRow.status != "retired")
        )
        return (await self._session.execute(stmt)).scalar_one_or_none() is not None


def _row_to_entity(row: PromptRow) -> PromptEntity:
    return PromptEntity(
        id=row.id,
        project_id=row.project_id,
        scene_id=row.scene_id,
        kind=row.kind,
        text_content=row.text_content,
        model_id=row.model_id,
        extra=dict(row.extra),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
