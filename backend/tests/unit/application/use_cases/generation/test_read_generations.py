"""Unit tests — α9.7 generation read model and cancel (ADR-0052 D3, pre-flight PF4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.application.pagination import decode_cursor
from app.application.use_cases.generation.read_generations import (
    CancelGeneration,
    GetGeneration,
    ListGenerations,
)
from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.domain.generation.execution_state import ExecutionStatus
from tests.unit.application.use_cases.generation._ingress_fakes import FakeGenerationJobStore

pytestmark = pytest.mark.unit


async def test_get_returns_the_callers_generation() -> None:
    store = FakeGenerationJobStore()
    row = store.seed()

    view = await GetGeneration(store).execute(
        tenant_id=row.tenant_id, owner_user_id=row.owner_user_id, generation_id=row.id
    )

    assert view.id == row.id


async def test_get_another_owners_generation_is_404() -> None:
    """Not-owned is reported exactly like not-found, so an id cannot be probed."""
    store = FakeGenerationJobStore()
    row = store.seed()

    with pytest.raises(NotFoundError):
        await GetGeneration(store).execute(
            tenant_id=row.tenant_id, owner_user_id=uuid4(), generation_id=row.id
        )


async def test_list_is_owner_scoped_and_newest_first() -> None:
    store = FakeGenerationJobStore()
    tenant_id, owner_user_id = uuid4(), uuid4()
    now = datetime.now(UTC)
    older = store.seed(
        tenant_id=tenant_id, owner_user_id=owner_user_id, created_at=now - timedelta(minutes=5)
    )
    newer = store.seed(tenant_id=tenant_id, owner_user_id=owner_user_id, created_at=now)
    store.seed()  # a different creator's generation

    page = await ListGenerations(store).execute(
        tenant_id=tenant_id, owner_user_id=owner_user_id, limit=10
    )

    assert [v.id for v in page.items] == [newer.id, older.id]
    assert page.next_cursor is None


async def test_list_pages_without_duplicates_or_gaps() -> None:
    store = FakeGenerationJobStore()
    tenant_id, owner_user_id = uuid4(), uuid4()
    now = datetime.now(UTC)
    for i in range(5):
        store.seed(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            created_at=now - timedelta(minutes=i),
        )
    uc = ListGenerations(store)

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(3):
        page = await uc.execute(
            tenant_id=tenant_id, owner_user_id=owner_user_id, limit=2, cursor_token=cursor
        )
        seen.extend(str(v.id) for v in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break

    assert len(seen) == 5
    assert len(set(seen)) == 5
    assert cursor is None


async def test_list_emits_a_cursor_only_when_more_remain() -> None:
    store = FakeGenerationJobStore()
    tenant_id, owner_user_id = uuid4(), uuid4()
    now = datetime.now(UTC)
    for i in range(3):
        store.seed(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            created_at=now - timedelta(minutes=i),
        )

    page = await ListGenerations(store).execute(
        tenant_id=tenant_id, owner_user_id=owner_user_id, limit=2
    )

    assert page.next_cursor is not None
    assert decode_cursor(page.next_cursor).id == page.items[-1].id


async def test_list_rejects_a_malformed_cursor() -> None:
    store = FakeGenerationJobStore()

    with pytest.raises(ValidationFailedError):
        await ListGenerations(store).execute(
            tenant_id=uuid4(), owner_user_id=uuid4(), limit=10, cursor_token="not-a-cursor"
        )


async def test_list_filters_by_status() -> None:
    store = FakeGenerationJobStore()
    tenant_id, owner_user_id = uuid4(), uuid4()
    store.seed(tenant_id=tenant_id, owner_user_id=owner_user_id)
    done = store.seed(
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        status=ExecutionStatus.COMPLETED.value,
    )

    page = await ListGenerations(store).execute(
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        limit=10,
        status=ExecutionStatus.COMPLETED.value,
    )

    assert [v.id for v in page.items] == [done.id]


async def test_cancel_a_queued_generation() -> None:
    store = FakeGenerationJobStore()
    row = store.seed()

    view = await CancelGeneration(store).execute(
        tenant_id=row.tenant_id, owner_user_id=row.owner_user_id, generation_id=row.id
    )

    assert view.status == ExecutionStatus.CANCELLED.value


async def test_cancel_a_claimed_generation_is_409() -> None:
    """PF4 — reporting success without stopping the work, or the spend, would be a lie."""
    store = FakeGenerationJobStore()
    row = store.seed(status=ExecutionStatus.GENERATING.value)

    with pytest.raises(ConflictError):
        await CancelGeneration(store).execute(
            tenant_id=row.tenant_id, owner_user_id=row.owner_user_id, generation_id=row.id
        )


async def test_cancel_a_terminal_generation_is_409() -> None:
    store = FakeGenerationJobStore()
    row = store.seed(status=ExecutionStatus.COMPLETED.value)

    with pytest.raises(ConflictError):
        await CancelGeneration(store).execute(
            tenant_id=row.tenant_id, owner_user_id=row.owner_user_id, generation_id=row.id
        )


async def test_cancel_another_owners_generation_is_404() -> None:
    store = FakeGenerationJobStore()
    row = store.seed()

    with pytest.raises(NotFoundError):
        await CancelGeneration(store).execute(
            tenant_id=row.tenant_id, owner_user_id=uuid4(), generation_id=row.id
        )
    assert store.rows[row.id].status == ExecutionStatus.QUEUED.value
