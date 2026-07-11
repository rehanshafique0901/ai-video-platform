"""Integration tests for ``/api/v1/projects`` (Slice α5a — create + read).

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
        json={"email": _fresh_email(), "password": "correct horse battery staple", "name": "P"},
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
