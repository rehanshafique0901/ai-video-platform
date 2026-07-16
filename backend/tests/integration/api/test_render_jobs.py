"""Integration tests for ``/api/v1/projects/{id}/render-jobs`` (α7.1).

End-to-end coverage through middleware, exception handlers, DI,
``get_current_user``, the real ``RenderJobRepository`` + ``EventOutboxRepository``,
and the live database. Every test uses the SAVEPOINT-rolled-back ``client``
fixture; nothing persists.

A **render job** is the request to render a project's timeline (ADR-0039). It is
**project-nested** (ownership via the project → ``404``), **self-versioned**
(``version`` on the wire; cancel is fenced on it), and needs a provisioned
timeline (a project without one → ``422`` on create). There is **no** worker in
α7.1 — created jobs stay ``queued``.

Coverage map (α7.1 pre-flight §5.4):

* B1  create happy                → 201 + ``RenderJobPublic`` (queued, version=1)
* B2  create without auth         → 401
* B3  create unknown project      → 404
* B4  create project w/o timeline → 422
* B5  create idempotent replay    → 201 then 200 (same id)
* B6  create custom pipeline/queue/priority → 201 echoes them
* B7  list empty                  → 200 []
* B8  list newest-first + status filter
* B9  list bad status enum        → 422
* B10 get happy / unknown         → 200 / 404
* B11 cancel queued               → 200 (canceled, version bumped)
* B12 cancel already canceled     → 200 no-op (same version)
* B13 cancel terminal (via seeded state is worker-only) — covered by repo tests
* B14 cancel stale version        → 412
* B15 cancel missing version body → 422
* B16 cross-owner isolation       → 404
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import AsyncClient

_RENDER_JOB_KEYS = {
    "id",
    "project_id",
    "timeline_id",
    "workflow_run_id",
    "pipeline",
    "pipeline_version",
    "queue",
    "priority",
    "status",
    "started_at",
    "finished_at",
    "progress",
    "error",
    "output_media_asset_id",
    "idempotency_key",
    "version",
    "created_at",
    "updated_at",
}


def _auth(access: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access}"}


async def _register(client: AsyncClient) -> dict:
    r = await client.post(
        "/api/v1/auth/register",
        json={
            "email": f"rj-{uuid4()}@example.com",
            "password": "correct horse battery staple",
            "name": "R",
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


async def _provision_timeline(client: AsyncClient, access: str, project_id: str) -> None:
    r = await client.post(
        f"/api/v1/projects/{project_id}/timeline",
        headers=_auth(access),
        json={},
    )
    assert r.status_code == 201, r.text


async def _setup(client: AsyncClient) -> tuple[str, str]:
    """Register a user, create a project + provisioned timeline. Returns (access, project_id)."""
    access = (await _register(client))["access_token"]
    project_id = await _create_project(client, access)
    await _provision_timeline(client, access, project_id)
    return access, project_id


async def _create_job(client: AsyncClient, access: str, project_id: str, **over: object) -> dict:
    r = await client.post(
        f"/api/v1/projects/{project_id}/render-jobs",
        headers=_auth(access),
        json=dict(over),
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]


# ---- B1 — create happy ------------------------------------------------


@pytest.mark.integration
async def test_b1_create_happy(client: AsyncClient) -> None:
    access, project_id = await _setup(client)

    r = await client.post(
        f"/api/v1/projects/{project_id}/render-jobs",
        headers=_auth(access),
        json={},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body.keys()) == {"data", "meta"}
    data = body["data"]
    assert set(data.keys()) == _RENDER_JOB_KEYS
    assert data["status"] == "queued"
    assert data["version"] == 1
    assert data["progress"] == "0.00"
    assert data["pipeline"] == "ffmpeg"
    assert data["pipeline_version"] == "0.0.0"
    assert data["queue"] == "normal"
    assert data["priority"] == 0
    assert data["project_id"] == project_id
    assert data["timeline_id"] is not None
    assert data["workflow_run_id"] is None
    assert data["output_media_asset_id"] is None
    assert data["error"] is None


# ---- B2 — create without auth → 401 -----------------------------------


@pytest.mark.integration
async def test_b2_create_without_auth(client: AsyncClient) -> None:
    access, project_id = await _setup(client)

    r = await client.post(f"/api/v1/projects/{project_id}/render-jobs", json={})
    assert r.status_code == 401, r.text


# ---- B3 — create unknown project → 404 --------------------------------


@pytest.mark.integration
async def test_b3_create_unknown_project(client: AsyncClient) -> None:
    access = (await _register(client))["access_token"]

    r = await client.post(f"/api/v1/projects/{uuid4()}/render-jobs", headers=_auth(access), json={})
    assert r.status_code == 404, r.text


# ---- B4 — create without a timeline → 422 -----------------------------


@pytest.mark.integration
async def test_b4_create_without_timeline(client: AsyncClient) -> None:
    access = (await _register(client))["access_token"]
    project_id = await _create_project(client, access)  # no timeline provisioned

    r = await client.post(
        f"/api/v1/projects/{project_id}/render-jobs", headers=_auth(access), json={}
    )
    assert r.status_code == 422, r.text


# ---- B5 — idempotent replay: 201 then 200 -----------------------------


@pytest.mark.integration
async def test_b5_idempotent_replay(client: AsyncClient) -> None:
    access, project_id = await _setup(client)

    first = await client.post(
        f"/api/v1/projects/{project_id}/render-jobs",
        headers=_auth(access),
        json={"idempotency_key": "run-once"},
    )
    assert first.status_code == 201, first.text
    first_id = first.json()["data"]["id"]

    second = await client.post(
        f"/api/v1/projects/{project_id}/render-jobs",
        headers=_auth(access),
        json={"idempotency_key": "run-once"},
    )
    assert second.status_code == 200, second.text
    assert second.json()["data"]["id"] == first_id


# ---- B6 — create echoes custom fields ---------------------------------


@pytest.mark.integration
async def test_b6_create_custom_fields(client: AsyncClient) -> None:
    access, project_id = await _setup(client)

    data = await _create_job(
        client,
        access,
        project_id,
        pipeline="remotion",
        pipeline_version="2.1.0",
        queue="high",
        priority=42,
    )
    assert data["pipeline"] == "remotion"
    assert data["pipeline_version"] == "2.1.0"
    assert data["queue"] == "high"
    assert data["priority"] == 42


# ---- B7 — list empty → 200 [] -----------------------------------------


@pytest.mark.integration
async def test_b7_list_empty(client: AsyncClient) -> None:
    access, project_id = await _setup(client)

    r = await client.get(f"/api/v1/projects/{project_id}/render-jobs", headers=_auth(access))
    assert r.status_code == 200, r.text
    assert r.json()["data"] == []


# ---- B8 — list newest-first + status filter ---------------------------


@pytest.mark.integration
async def test_b8_list_newest_first_and_status_filter(client: AsyncClient) -> None:
    access, project_id = await _setup(client)
    first = await _create_job(client, access, project_id)
    second = await _create_job(client, access, project_id)

    r = await client.get(f"/api/v1/projects/{project_id}/render-jobs", headers=_auth(access))
    assert r.status_code == 200, r.text
    rows = r.json()["data"]
    # Both jobs come back.
    assert {j["id"] for j in rows} == {first["id"], second["id"]}
    # ``created_at`` defaults to ``now()``, which Postgres holds CONSTANT for the
    # whole (single) test transaction, so the two rows tie on time and the
    # endpoint's ``created_at DESC, id DESC`` order reduces to the deterministic
    # ``id DESC`` tiebreak. Assert the endpoint applied that total order (i.e. it
    # sorts, rather than returning insertion/arbitrary order). True
    # newest-first-by-time is proven deterministically at the repository layer
    # (``test_r5``), where the ORDER BY lives and distinct ``created_at`` can be
    # stamped.
    returned_ids = [j["id"] for j in rows]
    expected_ids = [
        j["id"] for j in sorted(rows, key=lambda j: (j["created_at"], j["id"]), reverse=True)
    ]
    assert returned_ids == expected_ids

    # Cancel one → filter by canceled.
    cancel = await client.post(
        f"/api/v1/projects/{project_id}/render-jobs/{first['id']}/cancel",
        headers=_auth(access),
        json={"version": first["version"]},
    )
    assert cancel.status_code == 200, cancel.text

    filtered = await client.get(
        f"/api/v1/projects/{project_id}/render-jobs?status=canceled",
        headers=_auth(access),
    )
    assert filtered.status_code == 200, filtered.text
    assert [j["id"] for j in filtered.json()["data"]] == [first["id"]]


# ---- B9 — list bad status enum → 422 ----------------------------------


@pytest.mark.integration
async def test_b9_list_bad_status_enum(client: AsyncClient) -> None:
    access, project_id = await _setup(client)

    r = await client.get(
        f"/api/v1/projects/{project_id}/render-jobs?status=bogus", headers=_auth(access)
    )
    assert r.status_code == 422, r.text


# ---- B10 — get happy / unknown → 200 / 404 ----------------------------


@pytest.mark.integration
async def test_b10_get_happy_and_unknown(client: AsyncClient) -> None:
    access, project_id = await _setup(client)
    job = await _create_job(client, access, project_id)

    ok = await client.get(
        f"/api/v1/projects/{project_id}/render-jobs/{job['id']}", headers=_auth(access)
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["data"]["id"] == job["id"]

    missing = await client.get(
        f"/api/v1/projects/{project_id}/render-jobs/{uuid4()}", headers=_auth(access)
    )
    assert missing.status_code == 404, missing.text


# ---- B11 — cancel queued → 200 (canceled, version bumped) -------------


@pytest.mark.integration
async def test_b11_cancel_queued(client: AsyncClient) -> None:
    access, project_id = await _setup(client)
    job = await _create_job(client, access, project_id)

    r = await client.post(
        f"/api/v1/projects/{project_id}/render-jobs/{job['id']}/cancel",
        headers=_auth(access),
        json={"version": job["version"]},
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["status"] == "canceled"
    assert data["version"] == job["version"] + 1


# ---- B12 — cancel already canceled → 200 no-op ------------------------


@pytest.mark.integration
async def test_b12_cancel_idempotent_noop(client: AsyncClient) -> None:
    access, project_id = await _setup(client)
    job = await _create_job(client, access, project_id)

    first = await client.post(
        f"/api/v1/projects/{project_id}/render-jobs/{job['id']}/cancel",
        headers=_auth(access),
        json={"version": job["version"]},
    )
    assert first.status_code == 200, first.text
    canceled_version = first.json()["data"]["version"]

    # Re-cancel (any version): idempotent 200 no-op, no further version bump.
    second = await client.post(
        f"/api/v1/projects/{project_id}/render-jobs/{job['id']}/cancel",
        headers=_auth(access),
        json={"version": canceled_version},
    )
    assert second.status_code == 200, second.text
    assert second.json()["data"]["status"] == "canceled"
    assert second.json()["data"]["version"] == canceled_version


# ---- B14 — cancel stale version → 412 ---------------------------------


@pytest.mark.integration
async def test_b14_cancel_stale_version(client: AsyncClient) -> None:
    access, project_id = await _setup(client)
    job = await _create_job(client, access, project_id)

    r = await client.post(
        f"/api/v1/projects/{project_id}/render-jobs/{job['id']}/cancel",
        headers=_auth(access),
        json={"version": job["version"] + 99},
    )
    assert r.status_code == 412, r.text


# ---- B15 — cancel missing version body → 422 --------------------------


@pytest.mark.integration
async def test_b15_cancel_missing_version(client: AsyncClient) -> None:
    access, project_id = await _setup(client)
    job = await _create_job(client, access, project_id)

    r = await client.post(
        f"/api/v1/projects/{project_id}/render-jobs/{job['id']}/cancel",
        headers=_auth(access),
        json={},
    )
    assert r.status_code == 422, r.text


# ---- B16 — cross-owner isolation → 404 --------------------------------


@pytest.mark.integration
async def test_b16_cross_owner_isolation(client: AsyncClient) -> None:
    owner_access, project_id = await _setup(client)
    job = await _create_job(client, owner_access, project_id)

    other_access = (await _register(client))["access_token"]

    # List, get, and cancel all yield 404 for a non-owner.
    listed = await client.get(
        f"/api/v1/projects/{project_id}/render-jobs", headers=_auth(other_access)
    )
    assert listed.status_code == 404, listed.text

    got = await client.get(
        f"/api/v1/projects/{project_id}/render-jobs/{job['id']}", headers=_auth(other_access)
    )
    assert got.status_code == 404, got.text

    canceled = await client.post(
        f"/api/v1/projects/{project_id}/render-jobs/{job['id']}/cancel",
        headers=_auth(other_access),
        json={"version": job["version"]},
    )
    assert canceled.status_code == 404, canceled.text
