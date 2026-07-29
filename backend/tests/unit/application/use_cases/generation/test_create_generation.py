"""Unit tests — α9.7 generation ingress (`CreateGeneration`, ADR-0052 D4).

The behaviour under test is the *semantics of asking twice*. Generation deliberately differs
from publishing here: a repeated prompt is a legitimate second take, so only an explicit
idempotency key collapses two requests into one.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.application.use_cases.generation.create_generation import (
    CreateGeneration,
    resolve_seed,
)
from app.application.use_cases.generation.request_codec import GenerationRequestSpec
from app.domain.generation.execution_state import ExecutionStatus
from tests.unit.application.use_cases.generation._ingress_fakes import FakeGenerationJobStore

pytestmark = pytest.mark.unit


def _spec(prompt: str = "a cat riding a skateboard") -> GenerationRequestSpec:
    return GenerationRequestSpec(prompt=prompt, seed=42)


async def test_create_queues_an_owned_generation() -> None:
    store = FakeGenerationJobStore()
    tenant_id, owner_user_id = uuid4(), uuid4()

    result = await CreateGeneration(store).execute(
        tenant_id=tenant_id, owner_user_id=owner_user_id, spec=_spec()
    )

    assert result.created is True
    assert result.generation.status == ExecutionStatus.QUEUED.value
    # Ingress records intent only — it never runs, and never pre-declares progress.
    assert result.generation.shots_accepted == 0
    assert result.generation.promotable is False
    row = store.rows[result.generation.id]
    assert (row.tenant_id, row.owner_user_id) == (tenant_id, owner_user_id)


async def test_same_key_replays_the_same_generation() -> None:
    store = FakeGenerationJobStore()
    uc = CreateGeneration(store)
    tenant_id, owner_user_id = uuid4(), uuid4()

    first = await uc.execute(
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        spec=_spec(),
        idempotency_key="k-1",
    )
    second = await uc.execute(
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        spec=_spec(),
        idempotency_key="k-1",
    )

    assert first.created is True
    assert second.created is False
    assert second.generation.id == first.generation.id
    assert len(store.rows) == 1


async def test_repeated_prompt_without_a_key_is_a_second_generation() -> None:
    """The iteration loop of a generative product: asking twice means asking twice."""
    store = FakeGenerationJobStore()
    uc = CreateGeneration(store)
    tenant_id, owner_user_id = uuid4(), uuid4()

    first = await uc.execute(tenant_id=tenant_id, owner_user_id=owner_user_id, spec=_spec())
    second = await uc.execute(tenant_id=tenant_id, owner_user_id=owner_user_id, spec=_spec())

    assert second.created is True
    assert second.generation.id != first.generation.id
    assert len(store.rows) == 2


async def test_a_new_key_is_a_new_generation() -> None:
    store = FakeGenerationJobStore()
    uc = CreateGeneration(store)
    tenant_id, owner_user_id = uuid4(), uuid4()

    await uc.execute(
        tenant_id=tenant_id, owner_user_id=owner_user_id, spec=_spec(), idempotency_key="k-1"
    )
    second = await uc.execute(
        tenant_id=tenant_id, owner_user_id=owner_user_id, spec=_spec(), idempotency_key="k-2"
    )

    assert second.created is True
    assert len(store.rows) == 2


async def test_keys_are_scoped_to_the_owner() -> None:
    store = FakeGenerationJobStore()
    uc = CreateGeneration(store)
    tenant_id = uuid4()

    await uc.execute(
        tenant_id=tenant_id, owner_user_id=uuid4(), spec=_spec(), idempotency_key="shared"
    )
    other = await uc.execute(
        tenant_id=tenant_id, owner_user_id=uuid4(), spec=_spec(), idempotency_key="shared"
    )

    # One creator's key must never collapse another creator's request.
    assert other.created is True
    assert len(store.rows) == 2


def test_resolve_seed_honours_the_caller_and_otherwise_draws_one() -> None:
    assert resolve_seed(7) == 7
    drawn = resolve_seed(None)
    assert 0 <= drawn < 2**31
