"""Unit tests for ``ListProjects`` (Slice α5a).

Coverage map (α5a pre-flight §8):

* U1 — empty result: ``items == []``, ``next_cursor is None``.
* U2 — single page (fewer rows than ``limit``): all rows returned
  newest-first, ``next_cursor is None``.
* U3 — multi-page keyset walk: paging with ``limit`` + ``next_cursor``
  returns every row exactly once, in ``created_at DESC, id DESC`` order,
  with no overlap and no gaps; the final page has ``next_cursor is None``
  (§D6 / §D14).
* U4 — owner + tenant scoping: rows owned by another user or in another
  tenant are excluded (§D5).
* U5 — a malformed cursor propagates ``ValidationFailedError`` (422) —
  the use case decodes the cursor before touching the repo.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.application.use_cases.projects.list_projects import ListProjects
from app.core.errors import ValidationFailedError
from app.domain.projects.project import Project
from tests.unit.application.use_cases.auth._fakes import (
    FakeProjectRepository,
    FakeUnitOfWork,
)

_BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _make_project(*, tenant_id: UUID, owner_user_id: UUID, seq: int) -> Project:
    """A project whose ``created_at`` increases with ``seq`` (higher = newer)."""
    ts = _BASE + timedelta(seconds=seq)
    return Project(
        id=uuid4(),
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        folder_id=None,
        current_version_id=None,
        name=f"P{seq}",
        description=None,
        aspect_ratio="horizontal",
        duration_seconds=None,
        language="en",
        style=None,
        settings={},
        created_at=ts,
        updated_at=ts,
        version=1,
    )


def _build(rows: list[Project]) -> ListProjects:
    repo = FakeProjectRepository()
    for r in rows:
        repo._rows[r.id] = r
    return ListProjects(uow=FakeUnitOfWork(projects=repo))


@pytest.mark.unit
async def test_u1_empty_returns_no_items_and_no_cursor() -> None:
    uc = _build([])
    page = await uc.execute(owner_user_id=uuid4(), tenant_id=uuid4(), limit=20)
    assert page.items == []
    assert page.next_cursor is None


@pytest.mark.unit
async def test_u2_single_page_newest_first_no_cursor() -> None:
    owner_id, tenant_id = uuid4(), uuid4()
    rows = [_make_project(tenant_id=tenant_id, owner_user_id=owner_id, seq=i) for i in range(3)]
    uc = _build(rows)

    page = await uc.execute(owner_user_id=owner_id, tenant_id=tenant_id, limit=20)

    assert page.next_cursor is None
    # Newest first: seq 2, 1, 0.
    assert [p.name for p in page.items] == ["P2", "P1", "P0"]


@pytest.mark.unit
async def test_u3_multipage_keyset_walk_covers_all_rows_in_order() -> None:
    owner_id, tenant_id = uuid4(), uuid4()
    rows = [_make_project(tenant_id=tenant_id, owner_user_id=owner_id, seq=i) for i in range(5)]
    uc = _build(rows)

    collected: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        page = await uc.execute(
            owner_user_id=owner_id, tenant_id=tenant_id, limit=2, cursor_token=cursor
        )
        collected.extend(p.name for p in page.items)
        pages += 1
        assert len(page.items) <= 2
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
        assert pages < 10, "pagination did not terminate"

    # Every row exactly once, newest-first, no overlap / no gap.
    assert collected == ["P4", "P3", "P2", "P1", "P0"]
    assert pages == 3  # 2 + 2 + 1


@pytest.mark.unit
async def test_u4_owner_and_tenant_scoping_excludes_foreign_rows() -> None:
    owner_id, tenant_id = uuid4(), uuid4()
    mine = _make_project(tenant_id=tenant_id, owner_user_id=owner_id, seq=1)
    other_owner = _make_project(tenant_id=tenant_id, owner_user_id=uuid4(), seq=2)
    other_tenant = _make_project(tenant_id=uuid4(), owner_user_id=owner_id, seq=3)
    uc = _build([mine, other_owner, other_tenant])

    page = await uc.execute(owner_user_id=owner_id, tenant_id=tenant_id, limit=20)

    assert [p.id for p in page.items] == [mine.id]


@pytest.mark.unit
async def test_u5_malformed_cursor_raises_validation_failed() -> None:
    uc = _build([])
    with pytest.raises(ValidationFailedError):
        await uc.execute(
            owner_user_id=uuid4(),
            tenant_id=uuid4(),
            limit=20,
            cursor_token="not-a-valid-cursor",
        )
