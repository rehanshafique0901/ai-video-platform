"""Unit tests — α9.7 generation ingress (`CreateGeneration`, ADR-0052 D4).

The behaviour under test is the *semantics of asking twice*. Generation deliberately differs
from publishing here: a repeated prompt is a legitimate second take, so only an explicit
idempotency key collapses two requests into one.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from app.application.use_cases.generation.create_generation import (
    CreateGeneration,
    resolve_seed,
)
from app.application.use_cases.generation.request_codec import GenerationRequestSpec
from app.application.use_cases.identity import CreateIdentityProfile, UpdateIdentityChild
from app.core.errors import NotFoundError
from app.domain.generation.execution_state import ExecutionStatus
from app.domain.generation.identity import GlobalStyle
from app.domain.identity_runtime import IdentityProfile
from tests.unit.application.use_cases.generation._ingress_fakes import FakeGenerationJobStore
from tests.unit.application.use_cases.identity._fakes import FakeIdentityUnitOfWork

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


# --- α10.0: binding an authored world -----------------------------------------


async def _world(
    uow: FakeIdentityUnitOfWork, *, tenant_id: UUID, owner_user_id: UUID, seed: int = 4242
) -> IdentityProfile:
    return await CreateIdentityProfile(uow).execute(
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        name="Bedtime",
        seed=seed,
        global_style=GlobalStyle.ANIME,
        camera_style="handheld",
        characters=[
            {"character_key": "zoe", "name": "Zoe", "appearance": ("curly red hair",)},
        ],
        locations=[{"location_key": "home", "name": "Home", "descriptors": ("cosy",)}],
        props=[{"prop_key": "kite", "name": "Kite"}],
    )


async def test_naming_a_world_snapshots_it_into_the_request() -> None:
    store, uow = FakeGenerationJobStore(), FakeIdentityUnitOfWork()
    tenant_id, owner_user_id = uuid4(), uuid4()
    profile = await _world(uow, tenant_id=tenant_id, owner_user_id=owner_user_id)

    result = await CreateGeneration(store, uow=uow).execute(
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        spec=_spec(),
        identity_id=profile.id,
    )

    snapshot = store.rows[result.generation.id].spec.identity
    assert snapshot is not None
    assert snapshot.identity_id == str(profile.id)
    assert snapshot.version == profile.version
    assert [c.key for c in snapshot.characters] == ["zoe"]
    assert snapshot.characters[0].appearance == ("curly red hair",)
    assert [loc.key for loc in snapshot.locations] == ["home"]
    assert [p.key for p in snapshot.props] == ["kite"]
    assert snapshot.camera_style == "handheld"


async def test_a_request_that_names_no_world_is_unchanged() -> None:
    store = FakeGenerationJobStore()

    result = await CreateGeneration(store).execute(
        tenant_id=uuid4(), owner_user_id=uuid4(), spec=_spec()
    )

    assert store.rows[result.generation.id].spec.identity is None


async def test_another_creator_s_world_is_a_404() -> None:
    store, uow = FakeGenerationJobStore(), FakeIdentityUnitOfWork()
    tenant_id = uuid4()
    profile = await _world(uow, tenant_id=tenant_id, owner_user_id=uuid4())

    with pytest.raises(NotFoundError):
        await CreateGeneration(store, uow=uow).execute(
            tenant_id=tenant_id,
            owner_user_id=uuid4(),
            spec=_spec(),
            identity_id=profile.id,
        )
    assert store.rows == {}


async def test_a_world_that_does_not_exist_is_a_404() -> None:
    store, uow = FakeGenerationJobStore(), FakeIdentityUnitOfWork()

    with pytest.raises(NotFoundError):
        await CreateGeneration(store, uow=uow).execute(
            tenant_id=uuid4(), owner_user_id=uuid4(), spec=_spec(), identity_id=uuid4()
        )


async def test_the_world_s_seed_is_used_when_the_caller_named_none() -> None:
    store, uow = FakeGenerationJobStore(), FakeIdentityUnitOfWork()
    tenant_id, owner_user_id = uuid4(), uuid4()
    profile = await _world(uow, tenant_id=tenant_id, owner_user_id=owner_user_id, seed=4242)

    result = await CreateGeneration(store, uow=uow).execute(
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        # What the router does when the body carried no seed: one was drawn.
        spec=GenerationRequestSpec(prompt="p", seed=resolve_seed(None)),
        identity_id=profile.id,
        requested_seed=None,
    )

    assert store.rows[result.generation.id].spec.seed == 4242


async def test_an_explicit_seed_outranks_the_world_s() -> None:
    store, uow = FakeGenerationJobStore(), FakeIdentityUnitOfWork()
    tenant_id, owner_user_id = uuid4(), uuid4()
    profile = await _world(uow, tenant_id=tenant_id, owner_user_id=owner_user_id, seed=4242)

    result = await CreateGeneration(store, uow=uow).execute(
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        spec=GenerationRequestSpec(prompt="p", seed=11),
        identity_id=profile.id,
        requested_seed=11,
    )

    row = store.rows[result.generation.id]
    assert row.spec.seed == 11
    # The world's own seed survives in the snapshot as what it declared.
    assert row.spec.identity is not None and row.spec.identity.seed == 4242


async def test_the_world_s_style_is_used_when_the_caller_named_none() -> None:
    """An authored style the run then ignores would be a control sold and discarded (IDENT-2)."""
    store, uow = FakeGenerationJobStore(), FakeIdentityUnitOfWork()
    tenant_id, owner_user_id = uuid4(), uuid4()
    profile = await _world(uow, tenant_id=tenant_id, owner_user_id=owner_user_id)

    result = await CreateGeneration(store, uow=uow).execute(
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        # What the router builds when the body named no style: the platform default.
        spec=GenerationRequestSpec(prompt="p", seed=1, global_style=GlobalStyle.PIXAR.value),
        identity_id=profile.id,
        requested_global_style=None,
    )

    assert store.rows[result.generation.id].spec.global_style == GlobalStyle.ANIME.value


async def test_an_explicit_style_outranks_the_world_s() -> None:
    store, uow = FakeGenerationJobStore(), FakeIdentityUnitOfWork()
    tenant_id, owner_user_id = uuid4(), uuid4()
    profile = await _world(uow, tenant_id=tenant_id, owner_user_id=owner_user_id)

    result = await CreateGeneration(store, uow=uow).execute(
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        spec=GenerationRequestSpec(prompt="p", seed=1, global_style=GlobalStyle.CLAYMATION.value),
        identity_id=profile.id,
        requested_global_style=GlobalStyle.CLAYMATION.value,
    )

    row = store.rows[result.generation.id]
    assert row.spec.global_style == GlobalStyle.CLAYMATION.value
    # The world's own style survives in the snapshot as what it declared.
    assert row.spec.identity is not None
    assert row.spec.identity.global_style == GlobalStyle.ANIME.value


async def test_a_replayed_key_keeps_its_original_world() -> None:
    """Identity is not part of the idempotency key (ADR-0055 frozen decision 14)."""
    store, uow = FakeGenerationJobStore(), FakeIdentityUnitOfWork()
    tenant_id, owner_user_id = uuid4(), uuid4()
    first_world = await _world(uow, tenant_id=tenant_id, owner_user_id=owner_user_id)
    second_world = await CreateIdentityProfile(uow).execute(
        tenant_id=tenant_id, owner_user_id=owner_user_id, name="Space", seed=9
    )
    uc = CreateGeneration(store, uow=uow)

    first = await uc.execute(
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        spec=_spec(),
        identity_id=first_world.id,
        idempotency_key="k-1",
    )
    second = await uc.execute(
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        spec=_spec(),
        identity_id=second_world.id,
        idempotency_key="k-1",
    )

    assert second.created is False
    assert second.generation.id == first.generation.id
    snapshot = store.rows[first.generation.id].spec.identity
    assert snapshot is not None and snapshot.identity_id == str(first_world.id)


async def test_editing_the_world_afterwards_cannot_reach_the_generation() -> None:
    """IDENT-1: a generation holds a value, not a reference."""
    store, uow = FakeGenerationJobStore(), FakeIdentityUnitOfWork()
    tenant_id, owner_user_id = uuid4(), uuid4()
    profile = await _world(uow, tenant_id=tenant_id, owner_user_id=owner_user_id)

    result = await CreateGeneration(store, uow=uow).execute(
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        spec=_spec(),
        identity_id=profile.id,
    )
    await UpdateIdentityChild(uow).execute(
        profile_id=profile.id,
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        expected_version=profile.version,
        kind="character",
        key="zoe",
        changes={"name": "Zoë the elder"},
    )

    snapshot = store.rows[result.generation.id].spec.identity
    assert snapshot is not None
    assert snapshot.characters[0].name == "Zoe"
    assert snapshot.version == profile.version
