"""Integration tests for ``/api/v1/projects/{project_id}/versions`` (α5d.1).

End-to-end coverage through middleware, exception handlers, DI,
``get_current_user``, the real ``ProjectVersionRepository`` (+ snapshot
assembly from the live project/storyboard/scenes), and the database. Every
test uses the SAVEPOINT-rolled-back ``client`` fixture; nothing persists.

Coverage map (α5d pre-flight §7 / §8):

* H1  create happy                 → 201 + ``ProjectVersionDetail`` (v#=1, manual_save)
* H2  create without auth          → 401
* H3  create on unowned/unknown project → 404
* H4  create with extra body field → 422 (``extra="forbid"``)
* H5  create captures scenes       → snapshot scenes match created ids/order
* H6  second create links parent   → v#=2, parent_version_id == first id
* H7  list empty                   → 200, ``data == []``
* H8  list newest-first metadata   → 200, v# [2,1], no snapshot in items
* H9  list unowned project         → 404
* H10 get happy                    → 200, ``ProjectVersionDetail`` with snapshot
* H11 get unknown version          → 404
* H12 get version of another project → 404 (cross-project isolation)
* H13 get non-UUID version path    → 422

Restore + diff (α5d.2 pre-flight §7 / §12):

* HR1 restore happy                → 200, new head (reason=restore, is_current),
                                      snapshot scenes reconciled to the source
* HR2 restore stale aggregate fence → 412 (no writes)
* HR3 restore unowned/unknown project or version → 404 (before the fence)
* HR4 restore bad body (missing/extra field) → 422
* HD1 diff happy                   → 200, coarse add/remove/modify counts
* HD2 diff missing ``against`` param → 422
* HD3 diff unknown version (either side) → 404

Branch (α5d.3 pre-flight §5 / §9):

* HB1 branch happy                 → 201, NEW ``ProjectPublic`` + ``meta.branched_from``;
                                      the new project is first-class (GET project/scenes/v1)
* HB2 branch bad body (missing/empty name, extra field) → 422
* HB3 branch unowned/unknown project or version → 404
* HB4 branch duplicate live name   → 409
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import AsyncClient

_PUBLIC_KEYS = {
    "id",
    "project_id",
    "version_number",
    "reason",
    "parent_version_id",
    "created_by_user_id",
    "created_at",
    "is_current",
}
_DETAIL_KEYS = _PUBLIC_KEYS | {"snapshot", "diff_summary"}

# ``ProjectPublic`` (the branch response) — matches ``schemas/projects.py``.
_PROJECT_PUBLIC_KEYS = {
    "id",
    "tenant_id",
    "owner_user_id",
    "folder_id",
    "name",
    "description",
    "aspect_ratio",
    "language",
    "style",
    "settings",
    "created_at",
    "updated_at",
    "version",
}


def _fresh_email() -> str:
    return f"version-{uuid4()}@example.com"


def _auth(access: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access}"}


async def _register(client: AsyncClient) -> dict:
    r = await client.post(
        "/api/v1/auth/register",
        json={
            "email": _fresh_email(),
            "password": "correct horse battery staple",
            "name": "V",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]


async def _create_project(client: AsyncClient, access: str) -> str:
    r = await client.post(
        "/api/v1/projects",
        headers=_auth(access),
        json={"name": f"Project {uuid4()}", "aspect_ratio": "horizontal"},
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


async def _create_scene(client: AsyncClient, access: str, project_id: str) -> str:
    r = await client.post(
        f"/api/v1/projects/{project_id}/scenes",
        headers=_auth(access),
        json={"title": f"Scene {uuid4()}", "duration_seconds": 5.0},
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


async def _create_version(client: AsyncClient, access: str, project_id: str) -> dict:
    r = await client.post(
        f"/api/v1/projects/{project_id}/versions",
        headers=_auth(access),
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]


async def _project_version(client: AsyncClient, access: str, project_id: str) -> int:
    """Read the project's live aggregate OCC token (the restore fence)."""
    r = await client.get(f"/api/v1/projects/{project_id}", headers=_auth(access))
    assert r.status_code == 200, r.text
    return r.json()["data"]["version"]


# ---- H1 — create happy -------------------------------------------------


@pytest.mark.integration
async def test_h1_create_happy_path(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)

    r = await client.post(f"/api/v1/projects/{project_id}/versions", headers=_auth(access))
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body.keys()) == {"data", "meta"}
    assert body["meta"]["request_id"]
    data = body["data"]
    assert set(data.keys()) == _DETAIL_KEYS
    assert data["version_number"] == 1
    assert data["reason"] == "manual_save"
    assert data["parent_version_id"] is None
    assert data["diff_summary"] is None
    assert data["project_id"] == project_id
    assert data["is_current"] is True
    assert data["snapshot"]["schema_version"] == 1
    assert data["snapshot"]["scenes"] == []


# ---- H2 — create without auth -----------------------------------------


@pytest.mark.integration
async def test_h2_create_without_auth_401(client: AsyncClient) -> None:
    r = await client.post(f"/api/v1/projects/{uuid4()}/versions")
    assert r.status_code == 401, r.text


# ---- H3 — create on unowned/unknown project ---------------------------


@pytest.mark.integration
async def test_h3_create_unowned_project_404(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    r = await client.post(f"/api/v1/projects/{uuid4()}/versions", headers=_auth(access))
    assert r.status_code == 404, r.text


# ---- H4 — create with extra body field --------------------------------


@pytest.mark.integration
async def test_h4_create_extra_field_422(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)
    r = await client.post(
        f"/api/v1/projects/{project_id}/versions",
        headers=_auth(access),
        json={"reason": "autosave"},  # server-owned — not client-supplied
    )
    assert r.status_code == 422, r.text


# ---- H5 — create captures scenes --------------------------------------


@pytest.mark.integration
async def test_h5_create_captures_scenes(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)
    scene_a = await _create_scene(client, access, project_id)
    scene_b = await _create_scene(client, access, project_id)

    data = await _create_version(client, access, project_id)
    snap_scene_ids = [s["id"] for s in data["snapshot"]["scenes"]]
    assert snap_scene_ids == [scene_a, scene_b]
    assert data["snapshot"]["storyboard"] is not None
    # Numeric duration serialized as a lossless string (α5d Q7).
    assert data["snapshot"]["scenes"][0]["duration_seconds"] == "5.000"


# ---- H6 — second create links parent ----------------------------------


@pytest.mark.integration
async def test_h6_second_create_links_parent(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)

    first = await _create_version(client, access, project_id)
    second = await _create_version(client, access, project_id)
    assert second["version_number"] == 2
    assert second["parent_version_id"] == first["id"]


# ---- H7 — list empty ---------------------------------------------------


@pytest.mark.integration
async def test_h7_list_empty(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)

    r = await client.get(f"/api/v1/projects/{project_id}/versions", headers=_auth(access))
    assert r.status_code == 200, r.text
    assert r.json()["data"] == []


# ---- H8 — list newest-first metadata ----------------------------------


@pytest.mark.integration
async def test_h8_list_newest_first_metadata(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)
    await _create_version(client, access, project_id)
    await _create_version(client, access, project_id)

    r = await client.get(f"/api/v1/projects/{project_id}/versions", headers=_auth(access))
    assert r.status_code == 200, r.text
    items = r.json()["data"]
    assert [i["version_number"] for i in items] == [2, 1]
    # LIST is metadata-only — no snapshot bodies (α5d Q4).
    assert set(items[0].keys()) == _PUBLIC_KEYS
    # Newest capture is current; the prior version is not (α5d pre-flight §4).
    assert items[0]["is_current"] is True
    assert items[1]["is_current"] is False


# ---- H9 — list unowned project ----------------------------------------


@pytest.mark.integration
async def test_h9_list_unowned_project_404(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    r = await client.get(f"/api/v1/projects/{uuid4()}/versions", headers=_auth(access))
    assert r.status_code == 404, r.text


# ---- H10 — get happy ---------------------------------------------------


@pytest.mark.integration
async def test_h10_get_happy_path(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)
    created = await _create_version(client, access, project_id)

    r = await client.get(
        f"/api/v1/projects/{project_id}/versions/{created['id']}",
        headers=_auth(access),
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert set(data.keys()) == _DETAIL_KEYS
    assert data["id"] == created["id"]
    assert data["is_current"] is True
    assert data["snapshot"]["schema_version"] == 1


# ---- H11 — get unknown version ----------------------------------------


@pytest.mark.integration
async def test_h11_get_unknown_version_404(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)
    r = await client.get(f"/api/v1/projects/{project_id}/versions/{uuid4()}", headers=_auth(access))
    assert r.status_code == 404, r.text


# ---- H12 — get version of another project -----------------------------


@pytest.mark.integration
async def test_h12_get_version_of_another_project_404(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_a = await _create_project(client, access)
    project_b = await _create_project(client, access)
    version_a = await _create_version(client, access, project_a)

    # The version belongs to project A; addressing it via project B → 404.
    r = await client.get(
        f"/api/v1/projects/{project_b}/versions/{version_a['id']}",
        headers=_auth(access),
    )
    assert r.status_code == 404, r.text


# ---- H13 — get non-UUID version path ----------------------------------


@pytest.mark.integration
async def test_h13_get_non_uuid_version_422(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)
    r = await client.get(
        f"/api/v1/projects/{project_id}/versions/not-a-uuid", headers=_auth(access)
    )
    assert r.status_code == 422, r.text


# ---- HR1 — restore happy path -----------------------------------------


@pytest.mark.integration
async def test_hr1_restore_happy_path(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)
    scene_a = await _create_scene(client, access, project_id)
    scene_b = await _create_scene(client, access, project_id)
    v1 = await _create_version(client, access, project_id)

    # Drift away from the snapshot: add a third scene after capture.
    await _create_scene(client, access, project_id)
    fence = await _project_version(client, access, project_id)

    r = await client.post(
        f"/api/v1/projects/{project_id}/versions/{v1['id']}/restore",
        headers=_auth(access),
        json={"version": fence},
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert set(data.keys()) == _DETAIL_KEYS
    assert data["reason"] == "restore"
    assert data["parent_version_id"] == v1["id"]
    assert data["version_number"] == v1["version_number"] + 1
    assert data["is_current"] is True
    # Live scene set reconciled back to the source snapshot (the added scene is
    # dropped; the two original ids survive in order).
    assert [s["id"] for s in data["snapshot"]["scenes"]] == [scene_a, scene_b]


# ---- HR2 — restore with a stale aggregate fence → 412 -----------------


@pytest.mark.integration
async def test_hr2_restore_stale_fence_412(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)
    v1 = await _create_version(client, access, project_id)
    fence = await _project_version(client, access, project_id)

    r = await client.post(
        f"/api/v1/projects/{project_id}/versions/{v1['id']}/restore",
        headers=_auth(access),
        json={"version": fence - 1},  # stale
    )
    assert r.status_code == 412, r.text


# ---- HR3 — restore project/version gate → 404 (before the fence) ------


@pytest.mark.integration
async def test_hr3_restore_unowned_or_unknown_404(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)
    v1 = await _create_version(client, access, project_id)

    # Unowned/unknown project → 404 even though the version id is real.
    r_project = await client.post(
        f"/api/v1/projects/{uuid4()}/versions/{v1['id']}/restore",
        headers=_auth(access),
        json={"version": 999},
    )
    assert r_project.status_code == 404, r_project.text

    # Unknown version under an owned project → 404 (before the fence).
    r_version = await client.post(
        f"/api/v1/projects/{project_id}/versions/{uuid4()}/restore",
        headers=_auth(access),
        json={"version": 999},
    )
    assert r_version.status_code == 404, r_version.text


# ---- HR4 — restore bad body → 422 -------------------------------------


@pytest.mark.integration
async def test_hr4_restore_bad_body_422(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)
    v1 = await _create_version(client, access, project_id)

    # Missing the required ``version`` fence.
    r_missing = await client.post(
        f"/api/v1/projects/{project_id}/versions/{v1['id']}/restore",
        headers=_auth(access),
        json={},
    )
    assert r_missing.status_code == 422, r_missing.text

    # Extra field (``extra="forbid"``).
    r_extra = await client.post(
        f"/api/v1/projects/{project_id}/versions/{v1['id']}/restore",
        headers=_auth(access),
        json={"version": 1, "reason": "hax"},
    )
    assert r_extra.status_code == 422, r_extra.text


# ---- HD1 — diff happy path --------------------------------------------


@pytest.mark.integration
async def test_hd1_diff_happy_path(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)
    scene_a = await _create_scene(client, access, project_id)
    scene_b = await _create_scene(client, access, project_id)
    v1 = await _create_version(client, access, project_id)

    # Mutate: modify scene A, remove scene B, add a new scene → then capture v2.
    await client.patch(
        f"/api/v1/projects/{project_id}/scenes/{scene_a}",
        headers=_auth(access),
        json={"version": 1, "title": "Modified"},
    )
    await client.delete(f"/api/v1/projects/{project_id}/scenes/{scene_b}", headers=_auth(access))
    await _create_scene(client, access, project_id)
    v2 = await _create_version(client, access, project_id)

    # Diff base=v1 (against) → target=v2 (path).
    r = await client.get(
        f"/api/v1/projects/{project_id}/versions/{v2['id']}/diff",
        headers=_auth(access),
        params={"against": v1["id"]},
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["base_version_number"] == v1["version_number"]
    assert data["target_version_number"] == v2["version_number"]
    assert data["scene_changes"]["added"] == 1
    assert data["scene_changes"]["removed"] == 1
    assert data["scene_changes"]["modified"] == 1


# ---- HD2 — diff missing ``against`` param → 422 -----------------------


@pytest.mark.integration
async def test_hd2_diff_missing_against_422(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)
    v1 = await _create_version(client, access, project_id)

    r = await client.get(
        f"/api/v1/projects/{project_id}/versions/{v1['id']}/diff", headers=_auth(access)
    )
    assert r.status_code == 422, r.text


# ---- HD3 — diff with an unknown version → 404 -------------------------


@pytest.mark.integration
async def test_hd3_diff_unknown_version_404(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)
    v1 = await _create_version(client, access, project_id)

    # Unknown ``against`` (base) → 404.
    r_base = await client.get(
        f"/api/v1/projects/{project_id}/versions/{v1['id']}/diff",
        headers=_auth(access),
        params={"against": str(uuid4())},
    )
    assert r_base.status_code == 404, r_base.text

    # Unknown target (path) → 404.
    r_target = await client.get(
        f"/api/v1/projects/{project_id}/versions/{uuid4()}/diff",
        headers=_auth(access),
        params={"against": v1["id"]},
    )
    assert r_target.status_code == 404, r_target.text


# ---- HB1 — branch happy path (fork to a new first-class project) ------


@pytest.mark.integration
async def test_hb1_branch_happy_path(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)
    scene_a = await _create_scene(client, access, project_id)
    scene_b = await _create_scene(client, access, project_id)
    v1 = await _create_version(client, access, project_id)

    new_name = f"Forked {uuid4()}"
    r = await client.post(
        f"/api/v1/projects/{project_id}/versions/{v1['id']}/branch",
        headers=_auth(access),
        json={"name": new_name},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    # Provenance breadcrumb echoed in meta (Q7).
    assert body["meta"]["branched_from"] == {
        "project_id": project_id,
        "version_id": v1["id"],
        "version_number": v1["version_number"],
    }
    new_project = body["data"]
    assert set(new_project.keys()) == _PROJECT_PUBLIC_KEYS
    assert new_project["id"] != project_id
    assert new_project["name"] == new_name
    assert new_project["aspect_ratio"] == "horizontal"
    assert new_project["version"] == 2  # created + first capture
    new_id = new_project["id"]

    # The new project is a first-class project: GET it directly.
    r_get = await client.get(f"/api/v1/projects/{new_id}", headers=_auth(access))
    assert r_get.status_code == 200, r_get.text

    # Its scenes are materialized with FRESH ids, content + order preserved.
    r_scenes = await client.get(f"/api/v1/projects/{new_id}/scenes", headers=_auth(access))
    assert r_scenes.status_code == 200, r_scenes.text
    new_scenes = r_scenes.json()["data"]
    assert len(new_scenes) == 2
    new_scene_ids = {s["id"] for s in new_scenes}
    assert new_scene_ids.isdisjoint({scene_a, scene_b})

    # Its version ledger holds exactly the reason=branch v1 (current) with the
    # persisted provenance block.
    r_versions = await client.get(f"/api/v1/projects/{new_id}/versions", headers=_auth(access))
    assert r_versions.status_code == 200, r_versions.text
    versions = r_versions.json()["data"]
    assert len(versions) == 1
    assert versions[0]["reason"] == "branch"
    assert versions[0]["version_number"] == 1
    assert versions[0]["parent_version_id"] is None
    assert versions[0]["is_current"] is True

    r_v1 = await client.get(
        f"/api/v1/projects/{new_id}/versions/{versions[0]['id']}", headers=_auth(access)
    )
    assert r_v1.status_code == 200, r_v1.text
    assert r_v1.json()["data"]["snapshot"]["branched_from"] == {
        "project_id": project_id,
        "version_id": v1["id"],
        "version_number": v1["version_number"],
    }


# ---- HB2 — branch bad body → 422 --------------------------------------


@pytest.mark.integration
async def test_hb2_branch_bad_body_422(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)
    v1 = await _create_version(client, access, project_id)
    url = f"/api/v1/projects/{project_id}/versions/{v1['id']}/branch"

    # Missing the required ``name``.
    r_missing = await client.post(url, headers=_auth(access), json={})
    assert r_missing.status_code == 422, r_missing.text

    # Empty / whitespace-only name (min_length after strip).
    r_empty = await client.post(url, headers=_auth(access), json={"name": "   "})
    assert r_empty.status_code == 422, r_empty.text

    # Extra field (``extra="forbid"``): root fields are inherited, not accepted.
    r_extra = await client.post(
        url, headers=_auth(access), json={"name": "OK", "aspect_ratio": "square"}
    )
    assert r_extra.status_code == 422, r_extra.text


# ---- HB3 — branch project/version gate → 404 --------------------------


@pytest.mark.integration
async def test_hb3_branch_unowned_or_unknown_404(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)
    v1 = await _create_version(client, access, project_id)

    # Unowned/unknown project → 404 even though the version id is real.
    r_project = await client.post(
        f"/api/v1/projects/{uuid4()}/versions/{v1['id']}/branch",
        headers=_auth(access),
        json={"name": "From nowhere"},
    )
    assert r_project.status_code == 404, r_project.text

    # Unknown version under an owned project → 404.
    r_version = await client.post(
        f"/api/v1/projects/{project_id}/versions/{uuid4()}/branch",
        headers=_auth(access),
        json={"name": "From unknown version"},
    )
    assert r_version.status_code == 404, r_version.text


# ---- HB4 — branch to a duplicate live name → 409 ----------------------


@pytest.mark.integration
async def test_hb4_branch_duplicate_name_409(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    project_id = await _create_project(client, access)
    v1 = await _create_version(client, access, project_id)

    taken = f"Taken {uuid4()}"
    r_first = await client.post(
        f"/api/v1/projects/{project_id}/versions/{v1['id']}/branch",
        headers=_auth(access),
        json={"name": taken},
    )
    assert r_first.status_code == 201, r_first.text

    # A second branch to the same live name for this owner → 409.
    r_dup = await client.post(
        f"/api/v1/projects/{project_id}/versions/{v1['id']}/branch",
        headers=_auth(access),
        json={"name": taken},
    )
    assert r_dup.status_code == 409, r_dup.text
