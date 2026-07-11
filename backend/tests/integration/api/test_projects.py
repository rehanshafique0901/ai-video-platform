"""Integration tests for ``/api/v1/projects`` (α5a create/read; α5b update/delete).

End-to-end coverage through middleware, exception handlers, DI,
``get_current_user``, the real ``ProjectRepository``, and the live
database. Every test uses the SAVEPOINT-rolled-back ``client`` fixture;
nothing persists between tests.

Note on ``created_at`` (H12/H13 ordering): the fixture holds one outer
transaction, so ``now()`` = ``transaction_timestamp()`` is constant —
every project created within a single test shares one ``created_at``.
Ordering therefore falls to the ``id DESC`` tie-break (α5a D14), which
is non-deterministic w.r.t. insertion order. These tests assert set
coverage + pagination correctness (no duplicate, no gap), not strict
insertion order — the ``created_at DESC`` primary sort is proven in the
repository test R6 (distinct staggered timestamps).

Coverage map (α5a pre-flight §8):

* H1  create happy                    → 201 + ``ProjectPublic`` (version=1) (A1)
* H2  create without auth             → 401 (A2)
* H3  create invalid aspect_ratio     → 422 (A5)
* H4  create extra field              → 422 (``extra="forbid"``) (A6)
* H5  create whitespace-only name     → 422 (A4)
* H6  create duplicate name           → 409 CONFLICT (A7)
* H7  get happy                       → 200, matches created (A8)
* H8  get unknown id                  → 404 (A11)
* H9  get another owner's project     → 404 (owner scoping, A12)
* H10 get non-UUID path               → 422
* H11 list empty                      → 200, ``data == []``, no next_cursor (A9)
* H12 list returns created projects   → 200, all present (A9)
* H13 list keyset pagination          → limit + next_cursor walk covers all (A10)
* H14 list invalid limit              → 422 (A14)
* H15 list invalid cursor             → 422
* H16 list without auth               → 401

α5b (pre-flight §5.3 P17–P32):

* H17 PATCH real change               → 200, field changed, version+1 (P17/A1)
* H18 PATCH envelope                  → data + meta.request_id (P18/A2)
* H19 PATCH partial (absent unchanged) → untouched fields preserved (P19/A3)
* H20 PATCH explicit-null clears / name:null → null / 422 (P20/A4)
* H21 PATCH same-value no-op          → 200, version unchanged (P21/A5)
* H22 PATCH stale version             → 412 VERSION_CONFLICT (P22/A6)
* H23 PATCH other user's project      → 404 (P23/A7)
* H24 PATCH unknown / soft-deleted id → 404 (P24/A7)
* H25 PATCH rename collision          → 409 (P25/A8)
* H26 PATCH forbidden / missing version / empty patch → 422 each (P26/A9-A11)
* H27 PATCH non-UUID path / no auth   → 422 / 401 (P27/A12-A13)
* H28 DELETE happy                    → 204; gone from list (P28/A14)
* H29 DELETE idempotent-by-404        → 2nd 404; GET/PATCH after → 404 (P29/A15)
* H30 DELETE other user's / unknown   → 404 (P30/A16)
* H31 DELETE frees name               → re-create same name → 201 (P31/A17)
* H32 DELETE no auth / non-UUID       → 401 / 422 (P32/A18)
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import AsyncClient

_PUBLIC_KEYS = {
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
    return f"proj-{uuid4()}@example.com"


async def _register(client: AsyncClient) -> dict:
    """Register a fresh user; return the ``data`` payload (user + tokens)."""
    r = await client.post(
        "/api/v1/auth/register",
        json={
            "email": _fresh_email(),
            "password": "correct horse battery staple",
            "name": "P",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]


async def _create(client: AsyncClient, access: str, **body: object) -> dict:
    payload = {"name": f"Project {uuid4()}", "aspect_ratio": "horizontal", **body}
    r = await client.post(
        "/api/v1/projects",
        headers={"Authorization": f"Bearer {access}"},
        json=payload,
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]


# ---- H1 — create happy ------------------------------------------------


@pytest.mark.integration
async def test_h1_create_happy_path(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]

    r = await client.post(
        "/api/v1/projects",
        headers={"Authorization": f"Bearer {access}"},
        json={"name": "My Video", "aspect_ratio": "vertical", "language": "en"},
    )

    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body.keys()) == {"data", "meta"}
    assert body["meta"]["request_id"]
    data = body["data"]
    assert set(data.keys()) == _PUBLIC_KEYS
    assert data["name"] == "My Video"
    assert data["aspect_ratio"] == "vertical"
    assert data["version"] == 1
    # Ownership + tenancy come from the authenticated caller (not the body).
    assert data["owner_user_id"] == reg["user"]["id"]
    assert data["tenant_id"] == reg["user"]["tenant_id"]
    # α5a-omitted fields must not leak (D8).
    assert "current_version_id" not in data
    assert "duration_seconds" not in data


# ---- H2 — create without auth ----------------------------------------


@pytest.mark.integration
async def test_h2_create_without_auth_returns_401(client: AsyncClient) -> None:
    r = await client.post("/api/v1/projects", json={"name": "X", "aspect_ratio": "horizontal"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHENTICATED"


# ---- H3–H5 — create validation (422) ----------------------------------


@pytest.mark.integration
async def test_h3_create_invalid_aspect_ratio_returns_422(client: AsyncClient) -> None:
    reg = await _register(client)
    r = await client.post(
        "/api/v1/projects",
        headers={"Authorization": f"Bearer {reg['access_token']}"},
        json={"name": "Bad AR", "aspect_ratio": "diagonal"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_FAILED"


@pytest.mark.integration
async def test_h4_create_extra_field_returns_422(client: AsyncClient) -> None:
    reg = await _register(client)
    r = await client.post(
        "/api/v1/projects",
        headers={"Authorization": f"Bearer {reg['access_token']}"},
        json={
            "name": "Sneaky",
            "aspect_ratio": "horizontal",
            "owner_user_id": str(uuid4()),  # forbidden — ownership is server-set
        },
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_FAILED"


@pytest.mark.integration
async def test_h5_create_whitespace_name_returns_422(client: AsyncClient) -> None:
    reg = await _register(client)
    r = await client.post(
        "/api/v1/projects",
        headers={"Authorization": f"Bearer {reg['access_token']}"},
        json={"name": "   ", "aspect_ratio": "horizontal"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_FAILED"


# ---- H6 — duplicate name → 409 ----------------------------------------


@pytest.mark.integration
async def test_h6_create_duplicate_name_returns_409(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    name = f"Unique {uuid4()}"

    first = await client.post(
        "/api/v1/projects",
        headers={"Authorization": f"Bearer {access}"},
        json={"name": name, "aspect_ratio": "horizontal"},
    )
    assert first.status_code == 201

    second = await client.post(
        "/api/v1/projects",
        headers={"Authorization": f"Bearer {access}"},
        json={"name": name, "aspect_ratio": "square"},
    )
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "CONFLICT"


# ---- H7–H10 — get -----------------------------------------------------


@pytest.mark.integration
async def test_h7_get_happy_path_matches_created(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    created = await _create(client, access, name="Fetchable")

    r = await client.get(
        f"/api/v1/projects/{created['id']}",
        headers={"Authorization": f"Bearer {access}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"] == created


@pytest.mark.integration
async def test_h8_get_unknown_id_returns_404(client: AsyncClient) -> None:
    reg = await _register(client)
    r = await client.get(
        f"/api/v1/projects/{uuid4()}",
        headers={"Authorization": f"Bearer {reg['access_token']}"},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.integration
async def test_h9_get_other_owners_project_returns_404(client: AsyncClient) -> None:
    """Owner scoping (D5): user B cannot read user A's project — the
    cross-owner read is indistinguishable from a missing row (404)."""
    reg_a = await _register(client)
    created = await _create(client, reg_a["access_token"], name="A's Project")

    reg_b = await _register(client)
    r = await client.get(
        f"/api/v1/projects/{created['id']}",
        headers={"Authorization": f"Bearer {reg_b['access_token']}"},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.integration
async def test_h10_get_non_uuid_path_returns_422(client: AsyncClient) -> None:
    reg = await _register(client)
    r = await client.get(
        "/api/v1/projects/not-a-uuid",
        headers={"Authorization": f"Bearer {reg['access_token']}"},
    )
    assert r.status_code == 422


# ---- H11–H13 — list ---------------------------------------------------


@pytest.mark.integration
async def test_h11_list_empty_returns_empty_data_no_cursor(client: AsyncClient) -> None:
    reg = await _register(client)
    r = await client.get(
        "/api/v1/projects", headers={"Authorization": f"Bearer {reg['access_token']}"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["data"] == []
    assert "next_cursor" not in body["meta"]


@pytest.mark.integration
async def test_h12_list_returns_created_projects(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    created_ids = {(await _create(client, access))["id"] for _ in range(3)}

    r = await client.get("/api/v1/projects", headers={"Authorization": f"Bearer {access}"})
    assert r.status_code == 200
    body = r.json()
    listed_ids = {p["id"] for p in body["data"]}
    assert created_ids == listed_ids
    assert "next_cursor" not in body["meta"]  # 3 < default limit 20


@pytest.mark.integration
async def test_h13_list_keyset_pagination_covers_all(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    created_ids = {(await _create(client, access))["id"] for _ in range(3)}

    # Page 1: limit=2 → 2 rows + a next_cursor.
    r1 = await client.get("/api/v1/projects?limit=2", headers={"Authorization": f"Bearer {access}"})
    assert r1.status_code == 200, r1.text
    b1 = r1.json()
    assert len(b1["data"]) == 2
    assert b1["meta"]["next_cursor"]
    seen = {p["id"] for p in b1["data"]}

    # Page 2: follow the cursor → the remaining row, no further cursor.
    r2 = await client.get(
        f"/api/v1/projects?limit=2&cursor={b1['meta']['next_cursor']}",
        headers={"Authorization": f"Bearer {access}"},
    )
    assert r2.status_code == 200, r2.text
    b2 = r2.json()
    assert len(b2["data"]) == 1
    assert "next_cursor" not in b2["meta"]
    seen |= {p["id"] for p in b2["data"]}

    # Every created row seen exactly once across the two pages, no gap.
    assert seen == created_ids


# ---- H14–H16 — list validation + auth ---------------------------------


@pytest.mark.integration
@pytest.mark.parametrize("limit", [0, 101, -1])
async def test_h14_list_invalid_limit_returns_422(client: AsyncClient, limit: int) -> None:
    reg = await _register(client)
    r = await client.get(
        f"/api/v1/projects?limit={limit}",
        headers={"Authorization": f"Bearer {reg['access_token']}"},
    )
    assert r.status_code == 422


@pytest.mark.integration
async def test_h15_list_invalid_cursor_returns_422(client: AsyncClient) -> None:
    reg = await _register(client)
    r = await client.get(
        "/api/v1/projects?cursor=not-a-valid-cursor",
        headers={"Authorization": f"Bearer {reg['access_token']}"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_FAILED"


@pytest.mark.integration
async def test_h16_list_without_auth_returns_401(client: AsyncClient) -> None:
    r = await client.get("/api/v1/projects")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHENTICATED"


# ---- α5b helpers ------------------------------------------------------


def _auth(access: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access}"}


# ---- H17–H18 — PATCH real change + envelope ---------------------------


@pytest.mark.integration
async def test_h17_patch_real_change_bumps_version(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    created = await _create(client, access, name="Before")

    r = await client.patch(
        f"/api/v1/projects/{created['id']}",
        headers=_auth(access),
        json={"name": "After", "version": created["version"]},
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["name"] == "After"
    assert data["version"] == created["version"] + 1  # exactly +1, no double-bump


@pytest.mark.integration
async def test_h18_patch_envelope_shape(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    created = await _create(client, access, name="Env")

    r = await client.patch(
        f"/api/v1/projects/{created['id']}",
        headers=_auth(access),
        json={"description": "note", "version": created["version"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body.keys()) == {"data", "meta"}
    assert body["meta"]["request_id"]
    assert set(body["data"].keys()) == _PUBLIC_KEYS


# ---- H19 — PATCH partial (absent unchanged) ---------------------------


@pytest.mark.integration
async def test_h19_patch_partial_leaves_absent_fields_unchanged(
    client: AsyncClient,
) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    created = await _create(
        client,
        access,
        name="Keep",
        description="keep-desc",
        language="fr",
        style="cinematic",
        settings={"fps": 30},
    )

    r = await client.patch(
        f"/api/v1/projects/{created['id']}",
        headers=_auth(access),
        json={"name": "Renamed", "version": created["version"]},
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["name"] == "Renamed"
    assert data["description"] == "keep-desc"
    assert data["language"] == "fr"
    assert data["style"] == "cinematic"
    assert data["settings"] == {"fps": 30}


# ---- H20 — PATCH explicit-null clears; name:null → 422 ----------------


@pytest.mark.integration
async def test_h20_patch_explicit_null_clears_and_name_null_422(
    client: AsyncClient,
) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    created = await _create(client, access, name="Nullable", description="to-clear")

    # description: null → clears (nullable column).
    r = await client.patch(
        f"/api/v1/projects/{created['id']}",
        headers=_auth(access),
        json={"description": None, "version": created["version"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["description"] is None

    # name: null → 422 (non-nullable column).
    r2 = await client.patch(
        f"/api/v1/projects/{created['id']}",
        headers=_auth(access),
        json={"name": None, "version": created["version"] + 1},
    )
    assert r2.status_code == 422
    assert r2.json()["error"]["code"] == "VALIDATION_FAILED"


# ---- H21 — PATCH same-value no-op -------------------------------------


@pytest.mark.integration
async def test_h21_patch_same_value_no_op_version_unchanged(
    client: AsyncClient,
) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    created = await _create(client, access, name="Steady")

    r = await client.patch(
        f"/api/v1/projects/{created['id']}",
        headers=_auth(access),
        json={"name": "Steady", "version": created["version"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["version"] == created["version"]  # unchanged


# ---- H22 — PATCH stale version → 412 ----------------------------------


@pytest.mark.integration
async def test_h22_patch_stale_version_returns_412(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    created = await _create(client, access, name="Fenced")

    r = await client.patch(
        f"/api/v1/projects/{created['id']}",
        headers=_auth(access),
        json={"name": "New", "version": created["version"] + 999},
    )
    assert r.status_code == 412, r.text
    assert r.json()["error"]["code"] == "VERSION_CONFLICT"


# ---- H23 — PATCH other user's project → 404 ---------------------------


@pytest.mark.integration
async def test_h23_patch_other_owners_project_returns_404(client: AsyncClient) -> None:
    reg_a = await _register(client)
    created = await _create(client, reg_a["access_token"], name="A's")

    reg_b = await _register(client)
    r = await client.patch(
        f"/api/v1/projects/{created['id']}",
        headers=_auth(reg_b["access_token"]),
        json={"name": "Hijack", "version": created["version"]},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


# ---- H24 — PATCH unknown / soft-deleted id → 404 ----------------------


@pytest.mark.integration
async def test_h24_patch_unknown_or_deleted_returns_404(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]

    # Unknown id.
    r = await client.patch(
        f"/api/v1/projects/{uuid4()}",
        headers=_auth(access),
        json={"name": "X", "version": 1},
    )
    assert r.status_code == 404

    # Soft-deleted id → 404 on PATCH.
    created = await _create(client, access, name="ToDelete")
    d = await client.delete(f"/api/v1/projects/{created['id']}", headers=_auth(access))
    assert d.status_code == 204
    r2 = await client.patch(
        f"/api/v1/projects/{created['id']}",
        headers=_auth(access),
        json={"name": "X", "version": created["version"]},
    )
    assert r2.status_code == 404


# ---- H25 — PATCH rename collision → 409 -------------------------------


@pytest.mark.integration
async def test_h25_patch_rename_collision_returns_409(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    await _create(client, access, name="Alpha")
    beta = await _create(client, access, name="Beta")

    r = await client.patch(
        f"/api/v1/projects/{beta['id']}",
        headers=_auth(access),
        json={"name": "Alpha", "version": beta["version"]},
    )
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "CONFLICT"


# ---- H26 — PATCH forbidden / missing version / empty patch → 422 ------


@pytest.mark.integration
async def test_h26_patch_forbidden_missing_version_empty_all_422(
    client: AsyncClient,
) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    created = await _create(client, access, name="Guarded")
    url = f"/api/v1/projects/{created['id']}"

    # Forbidden field (aspect_ratio immutable in α5b; extra="forbid").
    r1 = await client.patch(
        url,
        headers=_auth(access),
        json={"aspect_ratio": "square", "version": created["version"]},
    )
    assert r1.status_code == 422

    # Missing version.
    r2 = await client.patch(url, headers=_auth(access), json={"name": "NoVersion"})
    assert r2.status_code == 422

    # Empty patch (only version, no mutable field).
    r3 = await client.patch(url, headers=_auth(access), json={"version": created["version"]})
    assert r3.status_code == 422


# ---- H27 — PATCH non-UUID path / no auth → 422 / 401 ------------------


@pytest.mark.integration
async def test_h27_patch_non_uuid_and_no_auth(client: AsyncClient) -> None:
    reg = await _register(client)
    # Non-UUID path.
    r1 = await client.patch(
        "/api/v1/projects/not-a-uuid",
        headers=_auth(reg["access_token"]),
        json={"name": "X", "version": 1},
    )
    assert r1.status_code == 422

    # No auth.
    r2 = await client.patch(f"/api/v1/projects/{uuid4()}", json={"name": "X", "version": 1})
    assert r2.status_code == 401
    assert r2.json()["error"]["code"] == "UNAUTHENTICATED"


# ---- H28 — DELETE happy → 204, gone from list -------------------------


@pytest.mark.integration
async def test_h28_delete_happy_removes_from_list(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    created = await _create(client, access, name="Deletable")

    d = await client.delete(f"/api/v1/projects/{created['id']}", headers=_auth(access))
    assert d.status_code == 204
    assert d.content == b""  # no body on 204

    lst = await client.get("/api/v1/projects", headers=_auth(access))
    listed_ids = {p["id"] for p in lst.json()["data"]}
    assert created["id"] not in listed_ids


# ---- H29 — DELETE idempotent-by-404 -----------------------------------


@pytest.mark.integration
async def test_h29_delete_idempotent_by_404(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    created = await _create(client, access, name="Once")

    first = await client.delete(f"/api/v1/projects/{created['id']}", headers=_auth(access))
    assert first.status_code == 204

    # Second DELETE → 404.
    second = await client.delete(f"/api/v1/projects/{created['id']}", headers=_auth(access))
    assert second.status_code == 404

    # GET / PATCH after delete → 404.
    g = await client.get(f"/api/v1/projects/{created['id']}", headers=_auth(access))
    assert g.status_code == 404
    p = await client.patch(
        f"/api/v1/projects/{created['id']}",
        headers=_auth(access),
        json={"name": "X", "version": created["version"]},
    )
    assert p.status_code == 404


# ---- H30 — DELETE other user's / unknown → 404 ------------------------


@pytest.mark.integration
async def test_h30_delete_other_owners_or_unknown_returns_404(
    client: AsyncClient,
) -> None:
    reg_a = await _register(client)
    created = await _create(client, reg_a["access_token"], name="A owns")

    reg_b = await _register(client)
    # B cannot delete A's project.
    r1 = await client.delete(
        f"/api/v1/projects/{created['id']}", headers=_auth(reg_b["access_token"])
    )
    assert r1.status_code == 404

    # Unknown id.
    r2 = await client.delete(f"/api/v1/projects/{uuid4()}", headers=_auth(reg_b["access_token"]))
    assert r2.status_code == 404


# ---- H31 — DELETE frees the name (α5a A5 regression) ------------------


@pytest.mark.integration
async def test_h31_delete_frees_name_for_recreate(client: AsyncClient) -> None:
    reg = await _register(client)
    access = reg["access_token"]
    name = f"Recyclable {uuid4()}"
    first = await _create(client, access, name=name)

    d = await client.delete(f"/api/v1/projects/{first['id']}", headers=_auth(access))
    assert d.status_code == 204

    # The partial-unique index excludes soft-deleted rows → re-create OK.
    r = await client.post(
        "/api/v1/projects",
        headers=_auth(access),
        json={"name": name, "aspect_ratio": "horizontal"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["data"]["id"] != first["id"]


# ---- H32 — DELETE no auth / non-UUID → 401 / 422 ----------------------


@pytest.mark.integration
async def test_h32_delete_no_auth_and_non_uuid(client: AsyncClient) -> None:
    # No auth.
    r1 = await client.delete(f"/api/v1/projects/{uuid4()}")
    assert r1.status_code == 401
    assert r1.json()["error"]["code"] == "UNAUTHENTICATED"

    # Non-UUID path.
    reg = await _register(client)
    r2 = await client.delete("/api/v1/projects/not-a-uuid", headers=_auth(reg["access_token"]))
    assert r2.status_code == 422
