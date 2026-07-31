"""Integration tests for α10.0 Identity-Runtime authoring against the live database.

Two halves, because the slice makes two distinct promises.

**The authoring surface** (``/api/v1/identities``) is driven over HTTP through middleware,
the exception handlers, DI, ``get_current_user``, the real ``IdentityRepository`` and the
live database: caps, per-owner name uniqueness, the root-fenced version every child write
bumps, 404-before-412, and the uniform 404 that hides another creator's world.

**The binding** is driven through the real ingress and worker persistence: authoring a
world, naming it on a generation, and then proving the three things IDENT-1 exists for —
that the request carries the world *whole*, that editing the world afterwards changes
nothing about the generation, and that deleting it leaves the past generation readable with
its provenance intact. The PF3 regression lives here too: ``begin()`` must no longer be able
to null the ``identity_id`` ingress wrote.

The provider pipeline is stubbed — a real run means ffmpeg and a remote model — but the
planner and prompt builder are the real, untouched ones (PF7), so the prompts asserted here
are the prompts a run would use.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import TracebackType
from typing import Any, Self
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.application.interfaces.generation_runner import IGenerationRunner
from app.application.use_cases.generation.create_generation import CreateGeneration
from app.application.use_cases.generation.generation_worker import GenerationWorker
from app.application.use_cases.generation.read_generations import GetGeneration
from app.application.use_cases.generation.request import GenerateVideoRequest
from app.application.use_cases.generation.request_codec import GenerationRequestSpec
from app.application.use_cases.generation.results import (
    GenerateVideoResult,
    GenerationProvenance,
    GenerationStatus,
)
from app.application.use_cases.identity import CreateIdentityProfile
from app.domain.generation.execution_state import ExecutionStatus
from app.domain.generation.identity import GlobalStyle
from app.domain.generation.planner import PlanRequest, plan_from_prompt
from app.domain.generation.prompt_builder import build_prompt
from app.infrastructure.db.models.identity import Tenant, User
from app.infrastructure.generation.execution_runtime_store import SqlExecutionRuntimeStore
from app.infrastructure.generation.generation_job_store import SqlGenerationJobStore
from app.infrastructure.repositories.distributed_lock_manager import (
    SqlAlchemyDistributedLockManager,
)
from app.infrastructure.repositories.identity_repository import IdentityRepository

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def bound(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A sessionmaker bound to one connection whose transaction rolls back on teardown."""
    async with engine.connect() as connection:
        outer_tx = await connection.begin()
        factory = async_sessionmaker(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield factory
        finally:
            await outer_tx.rollback()


class _Uow:
    """The two ports these tests need: the lock manager and the identity repository."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> Self:
        self._session = self._factory()
        self.locks = SqlAlchemyDistributedLockManager(self._session)
        self.identities = IdentityRepository(self._session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def commit(self) -> None:
        assert self._session is not None
        await self._session.commit()


async def _seed_owner(factory: async_sessionmaker[AsyncSession]) -> tuple[UUID, UUID]:
    tenant_id, user_id = uuid4(), uuid4()
    async with factory() as session:
        await session.execute(
            insert(Tenant).values(id=tenant_id, name="World", slug=f"world-{tenant_id}")
        )
        await session.execute(
            insert(User).values(
                id=user_id,
                tenant_id=tenant_id,
                email=f"world-{user_id}@example.com",
                display_name="World Owner",
            )
        )
        await session.commit()
    return tenant_id, user_id


async def _author_world(
    factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: UUID,
    owner_user_id: UUID,
    name: str = "Bedtime",
    seed: int = 4242,
) -> Any:
    return await CreateIdentityProfile(_Uow(factory)).execute(  # type: ignore[arg-type]
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        name=name,
        seed=seed,
        global_style=GlobalStyle.ANIME,
        camera_style="handheld",
        lighting="golden hour",
        characters=[
            {
                "character_key": "zoe",
                "name": "Zoe",
                "age": "7 years old",
                "appearance": ["curly red hair"],
                "clothing": "yellow raincoat",
            }
        ],
        locations=[{"location_key": "home", "name": "Rainy street", "descriptors": ["puddles"]}],
        props=[{"prop_key": "boat", "name": "Paper boat"}],
    )


class _WorldRunner(IGenerationRunner):
    """Stubs the provider work, drives the real planner, prompt builder and persistence."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._store = SqlExecutionRuntimeStore(factory)
        self.prompts: list[str] = []
        self.identities: list[Any] = []

    async def run(self, request: GenerateVideoRequest) -> GenerateVideoResult:
        generation_id = request.generation_id
        assert generation_id is not None
        self.identities.append(request.identity)
        plan = plan_from_prompt(
            PlanRequest(
                prompt=request.prompt,
                identity=request.identity,
                target_duration_seconds=request.target_duration_seconds,
                per_shot_seconds=request.per_shot_seconds,
            )
        )
        self.prompts = [
            build_prompt(
                request.identity,
                description=shot.description,
                character_ids=shot.character_ids,
                location_id=shot.location_id,
                intent=shot.intent,
            )
            for shot in plan.shots
        ]
        provenance = GenerationProvenance(
            generation_id=generation_id,
            capability="image.generate",
            execution_mode="auto",
            resolver_version="test-resolver",
            chosen_adapter="test-adapter",
            chosen_provider="test-provider",
            planner_version="p1",
        )
        await self._store.begin(
            generation_id=generation_id,
            request=request,
            provenance=provenance,
            title=request.title or "untitled",
            shot_count=len(plan.shots),
        )
        return GenerateVideoResult(
            status=GenerationStatus.SUCCEEDED,
            generation_id=generation_id,
            title=request.title or "untitled",
            provenance=provenance,
        )


def _worker(
    factory: async_sessionmaker[AsyncSession], runner: IGenerationRunner
) -> GenerationWorker:
    return GenerationWorker(
        uow=_Uow(factory),  # type: ignore[arg-type]
        store=SqlGenerationJobStore(factory),
        runner=runner,
    )


async def _read_row(factory: async_sessionmaker[AsyncSession], generation_id: UUID) -> Any:
    async with factory() as session:
        return (
            (
                await session.execute(
                    text("SELECT identity_id, seed, request FROM generations WHERE id = :id"),
                    {"id": str(generation_id)},
                )
            )
            .mappings()
            .one()
        )


# --------------------------------------------------------------------------- #
# the authoring surface, over HTTP
# --------------------------------------------------------------------------- #


async def _register(client: AsyncClient) -> dict[str, Any]:
    r = await client.post(
        "/api/v1/auth/register",
        json={
            "email": f"identity-{uuid4()}@example.com",
            "password": "correct horse battery staple",
            "name": "Creator",
        },
    )
    assert r.status_code == 201, r.text
    data: dict[str, Any] = r.json()["data"]
    return data


def _auth(access: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access}"}


async def _post_world(client: AsyncClient, access: str, **body: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": f"World {uuid4()}", **body}
    r = await client.post("/api/v1/identities", headers=_auth(access), json=payload)
    assert r.status_code == 201, r.text
    data: dict[str, Any] = r.json()["data"]
    return data


async def test_authoring_a_world_returns_it_whole(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]

    r = await client.post(
        "/api/v1/identities",
        headers=_auth(access),
        json={
            "name": "Bedtime",
            "seed": 4242,
            "global_style": "anime",
            "camera_style": "handheld",
            "characters": [
                {"character_key": "zoe", "name": "Zoe", "appearance": ["curly red hair"]}
            ],
            "locations": [{"location_key": "home", "name": "Rainy street"}],
            "props": [{"prop_key": "boat", "name": "Paper boat"}],
        },
    )

    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["version"] == 1
    assert data["seed"] == 4242
    assert data["global_style"] == "anime"
    assert [c["character_key"] for c in data["characters"]] == ["zoe"]
    assert data["characters"][0]["appearance"] == ["curly red hair"]
    # Ownership comes from the caller, never the body.
    assert data["owner_user_id"] == reg["user"]["id"]
    assert data["tenant_id"] == reg["user"]["tenant_id"]


async def test_a_world_without_a_seed_gets_a_stable_one(client: AsyncClient) -> None:
    reg = await _register(client)
    world = await _post_world(client, reg["access_token"])

    assert 0 <= world["seed"] < 2**31

    r = await client.get(f"/api/v1/identities/{world['id']}", headers=_auth(reg["access_token"]))
    assert r.json()["data"]["seed"] == world["seed"]


async def test_a_duplicate_name_is_a_conflict(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    world = await _post_world(client, access, name="Bedtime")

    r = await client.post("/api/v1/identities", headers=_auth(access), json={"name": "Bedtime"})

    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "CONFLICT"
    assert world["name"] == "Bedtime"


async def test_a_fifth_character_is_refused(client: AsyncClient) -> None:
    """The cap is the planner's behaviour, not a product preference (PF6)."""
    reg = await _register(client)

    r = await client.post(
        "/api/v1/identities",
        headers=_auth(reg["access_token"]),
        json={
            "name": "Crowded",
            "characters": [{"character_key": f"c{i}", "name": f"C{i}"} for i in range(5)],
        },
    )

    assert r.status_code == 422, r.text


async def test_a_reference_image_is_refused_rather_than_ignored(client: AsyncClient) -> None:
    """PF5: no executable adapter consumes one, so authoring must not sell it."""
    reg = await _register(client)

    r = await client.post(
        "/api/v1/identities",
        headers=_auth(reg["access_token"]),
        json={"name": "Anchored", "reference_image_refs": ["s3://face.png"]},
    )

    assert r.status_code == 422, r.text


async def test_authoring_requires_authentication(client: AsyncClient) -> None:
    r = await client.post("/api/v1/identities", json={"name": "Anonymous"})

    assert r.status_code == 401


async def test_another_creator_s_world_is_a_uniform_404(client: AsyncClient) -> None:
    owner = await _register(client)
    stranger = await _register(client)
    world = await _post_world(client, owner["access_token"])

    r = await client.get(
        f"/api/v1/identities/{world['id']}", headers=_auth(stranger["access_token"])
    )

    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


async def test_the_list_is_owner_scoped_and_pages(client: AsyncClient) -> None:
    owner = await _register(client)
    stranger = await _register(client)
    await _post_world(client, owner["access_token"], name="One")
    await _post_world(client, owner["access_token"], name="Two")
    await _post_world(client, stranger["access_token"], name="Theirs")

    first = await client.get(
        "/api/v1/identities", headers=_auth(owner["access_token"]), params={"limit": 1}
    )
    assert first.status_code == 200
    assert len(first.json()["data"]) == 1
    cursor = first.json()["meta"].get("next_cursor")
    assert cursor

    second = await client.get(
        "/api/v1/identities",
        headers=_auth(owner["access_token"]),
        params={"limit": 10, "cursor": cursor},
    )
    names = {w["name"] for w in first.json()["data"]} | {w["name"] for w in second.json()["data"]}
    assert names == {"One", "Two"}


async def test_a_root_edit_is_version_fenced(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    world = await _post_world(client, access)

    stale = await client.patch(
        f"/api/v1/identities/{world['id']}",
        headers=_auth(access),
        json={"version": world["version"] + 5, "lighting": "moonlight"},
    )
    assert stale.status_code == 412
    assert stale.json()["error"]["code"] == "VERSION_CONFLICT"

    ok = await client.patch(
        f"/api/v1/identities/{world['id']}",
        headers=_auth(access),
        json={"version": world["version"], "lighting": "moonlight"},
    )
    assert ok.status_code == 200
    assert ok.json()["data"]["lighting"] == "moonlight"
    assert ok.json()["data"]["version"] == world["version"] + 1


async def test_a_stranger_meets_404_before_412(client: AsyncClient) -> None:
    owner = await _register(client)
    stranger = await _register(client)
    world = await _post_world(client, owner["access_token"])

    r = await client.patch(
        f"/api/v1/identities/{world['id']}",
        headers=_auth(stranger["access_token"]),
        json={"version": 99, "lighting": "moonlight"},
    )

    assert r.status_code == 404


async def test_children_are_added_edited_and_removed_through_the_root(
    client: AsyncClient,
) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    world = await _post_world(client, access)
    wid = world["id"]

    added = await client.post(
        f"/api/v1/identities/{wid}/characters",
        headers=_auth(access),
        json={"version": 1, "character_key": "zoe", "name": "Zoe", "appearance": ["red hair"]},
    )
    assert added.status_code == 201, added.text
    assert added.json()["data"]["version"] == 2

    duplicate = await client.post(
        f"/api/v1/identities/{wid}/characters",
        headers=_auth(access),
        json={"version": 2, "character_key": "zoe", "name": "Zoe again"},
    )
    assert duplicate.status_code == 409

    edited = await client.patch(
        f"/api/v1/identities/{wid}/characters/zoe",
        headers=_auth(access),
        json={"version": 2, "clothing": "yellow raincoat"},
    )
    assert edited.status_code == 200
    assert edited.json()["data"]["characters"][0]["clothing"] == "yellow raincoat"
    assert edited.json()["data"]["version"] == 3

    unknown = await client.patch(
        f"/api/v1/identities/{wid}/characters/nobody",
        headers=_auth(access),
        json={"version": 3, "name": "Nobody"},
    )
    assert unknown.status_code == 404

    removed = await client.delete(
        f"/api/v1/identities/{wid}/characters/zoe",
        headers=_auth(access),
        params={"version": 3},
    )
    assert removed.status_code == 200
    assert removed.json()["data"]["characters"] == []
    assert removed.json()["data"]["version"] == 4


async def test_a_second_location_is_refused(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    world = await _post_world(client, access, locations=[{"location_key": "home", "name": "Home"}])

    r = await client.post(
        f"/api/v1/identities/{world['id']}/locations",
        headers=_auth(access),
        json={"version": 1, "location_key": "park", "name": "Park"},
    )

    assert r.status_code == 422


async def test_deleting_a_world_takes_its_children_with_it(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    world = await _post_world(client, access, characters=[{"character_key": "zoe", "name": "Zoe"}])

    gone = await client.delete(f"/api/v1/identities/{world['id']}", headers=_auth(access))
    assert gone.status_code == 204

    assert (
        await client.get(f"/api/v1/identities/{world['id']}", headers=_auth(access))
    ).status_code == 404
    # PF10: the name is free again, so the creator can start that world over.
    again = await client.post(
        "/api/v1/identities", headers=_auth(access), json={"name": world["name"]}
    )
    assert again.status_code == 201


# --------------------------------------------------------------------------- #
# binding a world to a generation
# --------------------------------------------------------------------------- #


async def test_the_request_carries_the_whole_world_and_its_provenance(
    bound: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, owner_user_id = await _seed_owner(bound)
    world = await _author_world(bound, tenant_id=tenant_id, owner_user_id=owner_user_id)
    store = SqlGenerationJobStore(bound)

    created = await CreateGeneration(store, uow=_Uow(bound)).execute(  # type: ignore[arg-type]
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        spec=GenerationRequestSpec(prompt="a paper boat on a rainy street", seed=1),
        identity_id=world.id,
        requested_seed=None,
    )

    row = await _read_row(bound, created.generation.id)
    assert row["identity_id"] == str(world.id)
    # The world's seed wins when the caller named none, and one value is persisted.
    assert row["seed"] == 4242
    payload = row["request"]
    assert payload["v"] == 2
    assert payload["seed"] == 4242
    snapshot = payload["identity"]
    assert snapshot["identity_id"] == str(world.id)
    assert snapshot["version"] == world.version
    assert [c["key"] for c in snapshot["characters"]] == ["zoe"]
    assert snapshot["characters"][0]["clothing"] == "yellow raincoat"
    assert [loc["key"] for loc in snapshot["locations"]] == ["home"]
    assert [p["key"] for p in snapshot["props"]] == ["boat"]


async def test_a_generation_naming_no_world_is_unchanged(
    bound: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, owner_user_id = await _seed_owner(bound)
    store = SqlGenerationJobStore(bound)

    created = await CreateGeneration(store, uow=_Uow(bound)).execute(  # type: ignore[arg-type]
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        spec=GenerationRequestSpec(prompt="a paper boat", seed=5),
    )

    row = await _read_row(bound, created.generation.id)
    assert row["identity_id"] is None
    assert "identity" not in row["request"]


async def test_the_run_sees_the_world_and_begin_cannot_null_its_provenance(
    bound: async_sessionmaker[AsyncSession],
) -> None:
    """The PF3 regression: ``identity_id`` is ingress-owned and survives execution."""
    tenant_id, owner_user_id = await _seed_owner(bound)
    world = await _author_world(bound, tenant_id=tenant_id, owner_user_id=owner_user_id)
    store = SqlGenerationJobStore(bound)
    created = await CreateGeneration(store, uow=_Uow(bound)).execute(  # type: ignore[arg-type]
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        spec=GenerationRequestSpec(prompt="a paper boat on a rainy street", seed=1),
        identity_id=world.id,
        requested_seed=None,
    )

    runner = _WorldRunner(bound)
    await _worker(bound, runner).run_once()

    # The pipeline received the authored world, rebuilt from the payload alone.
    identity = runner.identities[0]
    assert [c.id for c in identity.characters] == ["zoe"]
    assert identity.seed == 4242
    # …and the untouched planner and prompt builder put it into every shot.
    assert runner.prompts
    assert all("Zoe" in prompt for prompt in runner.prompts)
    assert all("Rainy street" in prompt for prompt in runner.prompts)
    assert all("Paper boat" in prompt for prompt in runner.prompts)

    row = await _read_row(bound, created.generation.id)
    assert row["identity_id"] == str(world.id)


async def test_editing_the_world_afterwards_changes_nothing(
    bound: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, owner_user_id = await _seed_owner(bound)
    world = await _author_world(bound, tenant_id=tenant_id, owner_user_id=owner_user_id)
    store = SqlGenerationJobStore(bound)
    created = await CreateGeneration(store, uow=_Uow(bound)).execute(  # type: ignore[arg-type]
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        spec=GenerationRequestSpec(prompt="a paper boat on a rainy street", seed=1),
        identity_id=world.id,
        requested_seed=None,
    )
    before = await _read_row(bound, created.generation.id)

    async with _Uow(bound) as uow:  # type: ignore[attr-defined]
        await uow.identities.update_character(
            world.id,
            tenant_id,
            owner_user_id,
            expected_version=world.version,
            character_key="zoe",
            changes={"name": "Zoë the elder", "clothing": "blue coat"},
        )
        await uow.commit()

    after = await _read_row(bound, created.generation.id)
    assert after["request"] == before["request"]
    assert after["request"]["identity"]["characters"][0]["name"] == "Zoe"
    assert after["request"]["identity"]["version"] == world.version


async def test_deleting_the_world_leaves_the_past_generation_intact(
    bound: async_sessionmaker[AsyncSession],
) -> None:
    """PF10 + IDENT-1: a dangling ``identity_id`` is the honest record, not a broken link."""
    tenant_id, owner_user_id = await _seed_owner(bound)
    world = await _author_world(bound, tenant_id=tenant_id, owner_user_id=owner_user_id)
    store = SqlGenerationJobStore(bound)
    created = await CreateGeneration(store, uow=_Uow(bound)).execute(  # type: ignore[arg-type]
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        spec=GenerationRequestSpec(prompt="a paper boat on a rainy street", seed=1),
        identity_id=world.id,
        requested_seed=None,
    )
    await _worker(bound, _WorldRunner(bound)).run_once()

    async with _Uow(bound) as uow:  # type: ignore[attr-defined]
        assert await uow.identities.delete_profile(world.id, tenant_id, owner_user_id)
        await uow.commit()

    view = await GetGeneration(store).execute(
        tenant_id=tenant_id, owner_user_id=owner_user_id, generation_id=created.generation.id
    )
    assert view.status in {ExecutionStatus.PLANNING.value, ExecutionStatus.COMPLETED.value}
    row = await _read_row(bound, created.generation.id)
    assert row["identity_id"] == str(world.id)
    assert row["request"]["identity"]["name"] == "Bedtime"
