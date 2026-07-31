"""Identity Runtime use-case unit tests (Slice α10.0, pre-flight §11).

Covers what the pre-flight names for this layer: 404-before-412, the OCC bump on child
writes, name uniqueness, and cap enforcement. Persistence itself is the repository's and
is proved against a live database by Stage 27.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.application.use_cases.identity import (
    AddIdentityChild,
    CreateIdentityProfile,
    DeleteIdentityProfile,
    GetIdentityProfile,
    ListIdentityProfiles,
    RemoveIdentityChild,
    UpdateIdentityChild,
    UpdateIdentityProfile,
)
from app.core.errors import (
    ConflictError,
    NotFoundError,
    ValidationFailedError,
    VersionConflictError,
)
from app.domain.generation.identity import GlobalStyle
from tests.unit.application.use_cases.identity._fakes import (
    FakeIdentityRepository,
    FakeIdentityUnitOfWork,
)

pytestmark = pytest.mark.unit

TENANT = uuid4()
OWNER = uuid4()
STRANGER = uuid4()


@pytest.fixture
def repo() -> FakeIdentityRepository:
    return FakeIdentityRepository()


@pytest.fixture
def uow(repo: FakeIdentityRepository) -> FakeIdentityUnitOfWork:
    return FakeIdentityUnitOfWork(identities=repo)


async def _world(uow: FakeIdentityUnitOfWork, name: str = "Bedtime", **kwargs: object) -> object:
    return await CreateIdentityProfile(uow).execute(
        tenant_id=TENANT, owner_user_id=OWNER, name=name, seed=42, **kwargs
    )


# ---- create --------------------------------------------------------------


async def test_create_returns_the_world_and_commits(uow: FakeIdentityUnitOfWork) -> None:
    profile = await CreateIdentityProfile(uow).execute(
        tenant_id=TENANT,
        owner_user_id=OWNER,
        name="Bedtime",
        seed=7,
        global_style=GlobalStyle.ANIME,
        characters=[{"character_key": "zoe", "name": "Zoe", "position": 1}],
        locations=[{"location_key": "home", "name": "Home"}],
    )
    assert profile.version == 1
    assert profile.seed == 7
    assert profile.global_style is GlobalStyle.ANIME
    assert [c.key for c in profile.characters] == ["zoe"]
    assert uow.committed


async def test_create_draws_a_seed_when_the_creator_gives_none(
    uow: FakeIdentityUnitOfWork,
) -> None:
    profile = await CreateIdentityProfile(uow).execute(
        tenant_id=TENANT, owner_user_id=OWNER, name="Bedtime"
    )
    assert 0 <= profile.seed < 2**31


async def test_create_rejects_a_duplicate_name_for_the_same_owner(
    uow: FakeIdentityUnitOfWork,
) -> None:
    await _world(uow)
    with pytest.raises(ConflictError):
        await _world(uow)


async def test_two_owners_may_use_the_same_name(uow: FakeIdentityUnitOfWork) -> None:
    await _world(uow)
    other = await CreateIdentityProfile(uow).execute(
        tenant_id=TENANT, owner_user_id=STRANGER, name="Bedtime", seed=1
    )
    assert other.name == "Bedtime"


async def test_create_enforces_the_character_cap(uow: FakeIdentityUnitOfWork) -> None:
    with pytest.raises(ValidationFailedError):
        await _world(
            uow,
            characters=[{"character_key": f"c{i}", "name": f"C{i}"} for i in range(5)],
        )


async def test_create_enforces_the_single_location_cap(uow: FakeIdentityUnitOfWork) -> None:
    with pytest.raises(ValidationFailedError):
        await _world(
            uow,
            locations=[
                {"location_key": "home", "name": "Home"},
                {"location_key": "park", "name": "Park"},
            ],
        )


async def test_create_rejects_two_children_sharing_a_key(uow: FakeIdentityUnitOfWork) -> None:
    with pytest.raises(ValidationFailedError):
        await _world(
            uow,
            characters=[
                {"character_key": "zoe", "name": "Zoe"},
                {"character_key": "zoe", "name": "Zoe again"},
            ],
        )


# ---- read ----------------------------------------------------------------


async def test_get_returns_the_owner_s_world(uow: FakeIdentityUnitOfWork) -> None:
    created = await _world(uow)
    got = await GetIdentityProfile(uow).execute(
        profile_id=created.id, tenant_id=TENANT, owner_user_id=OWNER
    )
    assert got.id == created.id


async def test_get_hides_another_creator_s_world_behind_a_404(
    uow: FakeIdentityUnitOfWork,
) -> None:
    created = await _world(uow)
    with pytest.raises(NotFoundError):
        await GetIdentityProfile(uow).execute(
            profile_id=created.id, tenant_id=TENANT, owner_user_id=STRANGER
        )


async def test_list_pages_newest_first(uow: FakeIdentityUnitOfWork) -> None:
    first = await _world(uow, name="One")
    second = await _world(uow, name="Two")
    page1 = await ListIdentityProfiles(uow).execute(tenant_id=TENANT, owner_user_id=OWNER, limit=1)
    assert [p.id for p in page1.items] == [second.id]
    assert page1.next_cursor is not None

    page2 = await ListIdentityProfiles(uow).execute(
        tenant_id=TENANT, owner_user_id=OWNER, limit=1, cursor_token=page1.next_cursor
    )
    assert [p.id for p in page2.items] == [first.id]
    assert page2.next_cursor is None


async def test_list_never_shows_another_creator_s_worlds(uow: FakeIdentityUnitOfWork) -> None:
    await _world(uow)
    page = await ListIdentityProfiles(uow).execute(
        tenant_id=TENANT, owner_user_id=STRANGER, limit=10
    )
    assert page.items == []


# ---- update root ---------------------------------------------------------


async def test_update_bumps_the_version(uow: FakeIdentityUnitOfWork) -> None:
    created = await _world(uow)
    updated = await UpdateIdentityProfile(uow).execute(
        profile_id=created.id,
        tenant_id=TENANT,
        owner_user_id=OWNER,
        expected_version=1,
        changes={"lighting": "golden hour"},
    )
    assert updated.lighting == "golden hour"
    assert updated.version == 2


async def test_update_is_404_before_412(uow: FakeIdentityUnitOfWork) -> None:
    created = await _world(uow)
    # A stale version on someone else's world must still read as "not found":
    # a stranger may not learn that the world exists.
    with pytest.raises(NotFoundError):
        await UpdateIdentityProfile(uow).execute(
            profile_id=created.id,
            tenant_id=TENANT,
            owner_user_id=STRANGER,
            expected_version=99,
            changes={"lighting": "x"},
        )


async def test_update_rejects_a_stale_version(uow: FakeIdentityUnitOfWork) -> None:
    created = await _world(uow)
    with pytest.raises(VersionConflictError):
        await UpdateIdentityProfile(uow).execute(
            profile_id=created.id,
            tenant_id=TENANT,
            owner_user_id=OWNER,
            expected_version=99,
            changes={"lighting": "x"},
        )


async def test_a_same_value_update_writes_nothing(uow: FakeIdentityUnitOfWork) -> None:
    created = await _world(uow, name="Bedtime")
    uow.committed = False
    unchanged = await UpdateIdentityProfile(uow).execute(
        profile_id=created.id,
        tenant_id=TENANT,
        owner_user_id=OWNER,
        expected_version=1,
        changes={"name": "Bedtime"},
    )
    assert unchanged.version == 1
    assert not uow.committed


async def test_update_translates_the_style_enum_for_persistence(
    uow: FakeIdentityUnitOfWork,
) -> None:
    created = await _world(uow)
    updated = await UpdateIdentityProfile(uow).execute(
        profile_id=created.id,
        tenant_id=TENANT,
        owner_user_id=OWNER,
        expected_version=1,
        changes={"global_style": GlobalStyle.CLAYMATION},
    )
    assert updated.global_style is GlobalStyle.CLAYMATION


async def test_renaming_onto_another_world_s_name_conflicts(
    uow: FakeIdentityUnitOfWork,
) -> None:
    await _world(uow, name="One")
    second = await _world(uow, name="Two")
    with pytest.raises(ConflictError):
        await UpdateIdentityProfile(uow).execute(
            profile_id=second.id,
            tenant_id=TENANT,
            owner_user_id=OWNER,
            expected_version=1,
            changes={"name": "One"},
        )


# ---- delete --------------------------------------------------------------


async def test_delete_removes_the_world(uow: FakeIdentityUnitOfWork) -> None:
    created = await _world(uow)
    await DeleteIdentityProfile(uow).execute(
        profile_id=created.id, tenant_id=TENANT, owner_user_id=OWNER
    )
    with pytest.raises(NotFoundError):
        await GetIdentityProfile(uow).execute(
            profile_id=created.id, tenant_id=TENANT, owner_user_id=OWNER
        )


async def test_delete_is_404_for_another_creator(uow: FakeIdentityUnitOfWork) -> None:
    created = await _world(uow)
    with pytest.raises(NotFoundError):
        await DeleteIdentityProfile(uow).execute(
            profile_id=created.id, tenant_id=TENANT, owner_user_id=STRANGER
        )


# ---- children ------------------------------------------------------------


async def test_adding_a_child_bumps_the_root(uow: FakeIdentityUnitOfWork) -> None:
    created = await _world(uow)
    updated = await AddIdentityChild(uow).execute(
        profile_id=created.id,
        tenant_id=TENANT,
        owner_user_id=OWNER,
        expected_version=1,
        kind="character",
        key="zoe",
        name="Zoe",
        attributes={"appearance": ("curly hair",)},
    )
    assert updated.version == 2
    assert updated.characters[0].appearance == ("curly hair",)


@pytest.mark.parametrize(
    ("kind", "cap"),
    [("character", 4), ("location", 1), ("prop", 6)],
)
async def test_a_child_add_stops_at_the_cap(
    uow: FakeIdentityUnitOfWork, kind: str, cap: int
) -> None:
    created = await _world(uow)
    version = 1
    for i in range(cap):
        profile = await AddIdentityChild(uow).execute(
            profile_id=created.id,
            tenant_id=TENANT,
            owner_user_id=OWNER,
            expected_version=version,
            kind=kind,  # type: ignore[arg-type]
            key=f"k{i}",
            name=f"N{i}",
        )
        version = profile.version
    with pytest.raises(ValidationFailedError):
        await AddIdentityChild(uow).execute(
            profile_id=created.id,
            tenant_id=TENANT,
            owner_user_id=OWNER,
            expected_version=version,
            kind=kind,  # type: ignore[arg-type]
            key="one-too-many",
            name="One too many",
        )


async def test_a_child_add_rejects_a_key_already_in_the_world(
    uow: FakeIdentityUnitOfWork,
) -> None:
    created = await _world(uow, characters=[{"character_key": "zoe", "name": "Zoe"}])
    with pytest.raises(ConflictError):
        await AddIdentityChild(uow).execute(
            profile_id=created.id,
            tenant_id=TENANT,
            owner_user_id=OWNER,
            expected_version=1,
            kind="character",
            key="zoe",
            name="Zoe again",
        )


async def test_a_child_add_rejects_a_stale_version(uow: FakeIdentityUnitOfWork) -> None:
    created = await _world(uow)
    with pytest.raises(VersionConflictError):
        await AddIdentityChild(uow).execute(
            profile_id=created.id,
            tenant_id=TENANT,
            owner_user_id=OWNER,
            expected_version=99,
            kind="prop",
            key="kite",
            name="Kite",
        )


async def test_a_child_add_to_a_missing_world_is_404(uow: FakeIdentityUnitOfWork) -> None:
    with pytest.raises(NotFoundError):
        await AddIdentityChild(uow).execute(
            profile_id=uuid4(),
            tenant_id=TENANT,
            owner_user_id=OWNER,
            expected_version=1,
            kind="prop",
            key="kite",
            name="Kite",
        )


async def test_updating_a_child_bumps_the_root(uow: FakeIdentityUnitOfWork) -> None:
    created = await _world(uow, props=[{"prop_key": "kite", "name": "Kite"}])
    updated = await UpdateIdentityChild(uow).execute(
        profile_id=created.id,
        tenant_id=TENANT,
        owner_user_id=OWNER,
        expected_version=1,
        kind="prop",
        key="kite",
        changes={"descriptors": ("red",)},
    )
    assert updated.version == 2
    assert updated.props[0].descriptors == ("red",)


async def test_a_same_value_child_update_writes_nothing(uow: FakeIdentityUnitOfWork) -> None:
    created = await _world(uow, props=[{"prop_key": "kite", "name": "Kite"}])
    uow.committed = False
    unchanged = await UpdateIdentityChild(uow).execute(
        profile_id=created.id,
        tenant_id=TENANT,
        owner_user_id=OWNER,
        expected_version=1,
        kind="prop",
        key="kite",
        changes={"name": "Kite"},
    )
    assert unchanged.version == 1
    assert not uow.committed


async def test_updating_an_unknown_child_is_404(uow: FakeIdentityUnitOfWork) -> None:
    created = await _world(uow)
    with pytest.raises(NotFoundError):
        await UpdateIdentityChild(uow).execute(
            profile_id=created.id,
            tenant_id=TENANT,
            owner_user_id=OWNER,
            expected_version=1,
            kind="character",
            key="nobody",
            changes={"name": "Nobody"},
        )


async def test_removing_a_child_bumps_the_root(uow: FakeIdentityUnitOfWork) -> None:
    created = await _world(
        uow,
        characters=[
            {"character_key": "zoe", "name": "Zoe"},
            {"character_key": "ben", "name": "Ben"},
        ],
    )
    updated = await RemoveIdentityChild(uow).execute(
        profile_id=created.id,
        tenant_id=TENANT,
        owner_user_id=OWNER,
        expected_version=1,
        kind="character",
        key="zoe",
    )
    assert updated.version == 2
    assert [c.key for c in updated.characters] == ["ben"]


async def test_removing_an_unknown_child_is_404(uow: FakeIdentityUnitOfWork) -> None:
    created = await _world(uow)
    with pytest.raises(NotFoundError):
        await RemoveIdentityChild(uow).execute(
            profile_id=created.id,
            tenant_id=TENANT,
            owner_user_id=OWNER,
            expected_version=1,
            kind="location",
            key="nowhere",
        )


async def test_a_child_write_by_a_stranger_is_404_not_412(
    uow: FakeIdentityUnitOfWork,
) -> None:
    created = await _world(uow, props=[{"prop_key": "kite", "name": "Kite"}])
    with pytest.raises(NotFoundError):
        await RemoveIdentityChild(uow).execute(
            profile_id=created.id,
            tenant_id=TENANT,
            owner_user_id=STRANGER,
            expected_version=99,
            kind="prop",
            key="kite",
        )
