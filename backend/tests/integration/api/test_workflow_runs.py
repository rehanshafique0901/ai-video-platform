"""Integration tests for ``/api/v1/projects/{id}/workflow-runs`` (α7.2).

End-to-end coverage through middleware, exception handlers, DI,
``get_current_user``, the real ``WorkflowRunRepository`` + ``EventOutboxRepository``,
the in-code workflow registry + deterministic runner, and the live database. Every
test uses the SAVEPOINT-rolled-back ``client`` fixture; nothing persists.

A **workflow run** is the record of one workflow execution (ADR-0040). It is
**project-nested** (ownership via the project → ``404``) and **status-guarded, not
version-fenced** — there is **no ``version``** on the wire and cancel/advance carry
no body (D3.2). The **synchronous deterministic runner** (``advance``) drives a run
to a terminal state within one call using pure, in-code step handlers (D3.11).

Coverage map (α7.2 pre-flight / Q3, Q7, Q8):

* B1  create happy                 → 201 + queued run + seeded pending steps
* B2  create without auth          → 401
* B3  create unknown project       → 404
* B4  create unknown workflow      → 422
* B5  create idempotent replay     → 201 then 200 (same id)
* B6  create extra/invalid body    → 422
* B7  list empty                   → 200 []
* B8  list newest-first + status filter
* B9  list bad status enum         → 422
* B10 get happy / unknown          → 200 / 404
* B11 advance noop-chain           → 200 succeeded + checkpoints + summary
* B12 advance retry-succeed        → 200 succeeded, flaky retries == 2
* B13 advance terminal-fail        → 200 failed (reason=terminal)
* B14 advance already terminal     → 409
* B15 cancel queued                → 200 canceled
* B16 cancel already canceled      → 200 no-op
* B17 cancel succeeded             → 409
* B18 cross-owner isolation        → 404
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import AsyncClient

_NOOP_CHAIN = "noop-chain"
_RETRY_SUCCEED = "retry-succeed"
_TERMINAL_FAIL = "terminal-fail"
_WF_VERSION = "1.0.0"

_RUN_KEYS = {
    "id",
    "project_id",
    "workflow_key",
    "workflow_version",
    "status",
    "started_at",
    "finished_at",
    "triggered_by_user_id",
    "idempotency_key",
    "output_summary",
    "error",
    "created_at",
    "updated_at",
    "input_snapshot",
    "steps",
    "latest_checkpoint",
}
_STEP_KEYS = {
    "id",
    "step_index",
    "step_name",
    "status",
    "started_at",
    "finished_at",
    "retries",
    "output",
    "error",
}


def _auth(access: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access}"}


async def _register(client: AsyncClient) -> dict:
    r = await client.post(
        "/api/v1/auth/register",
        json={
            "email": f"wf-{uuid4()}@example.com",
            "password": "correct horse battery staple",
            "name": "W",
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


async def _setup(client: AsyncClient) -> tuple[str, str]:
    """Register a user + create a project. Returns (access, project_id)."""
    access = (await _register(client))["access_token"]
    project_id = await _create_project(client, access)
    return access, project_id


async def _create_run(
    client: AsyncClient,
    access: str,
    project_id: str,
    *,
    workflow_key: str = _NOOP_CHAIN,
    **over: object,
) -> dict:
    r = await client.post(
        f"/api/v1/projects/{project_id}/workflow-runs",
        headers=_auth(access),
        json={"workflow_key": workflow_key, "workflow_version": _WF_VERSION, **over},
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]


# ---- B1 — create happy ------------------------------------------------


@pytest.mark.integration
async def test_b1_create_happy(client: AsyncClient) -> None:
    access, project_id = await _setup(client)

    r = await client.post(
        f"/api/v1/projects/{project_id}/workflow-runs",
        headers=_auth(access),
        json={"workflow_key": _NOOP_CHAIN, "workflow_version": _WF_VERSION},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body.keys()) == {"data", "meta"}
    data = body["data"]
    assert set(data.keys()) == _RUN_KEYS
    assert data["status"] == "queued"
    assert data["workflow_key"] == _NOOP_CHAIN
    assert data["workflow_version"] == _WF_VERSION
    assert data["project_id"] == project_id
    assert data["started_at"] is None
    assert data["finished_at"] is None
    assert data["output_summary"] is None
    assert data["error"] is None
    assert data["latest_checkpoint"] is None
    # noop-chain seeds three ordered pending steps.
    assert [s["step_index"] for s in data["steps"]] == [0, 1, 2]
    assert [s["step_name"] for s in data["steps"]] == ["extract", "transform", "summarize"]
    assert all(s["status"] == "pending" for s in data["steps"])
    assert set(data["steps"][0].keys()) == _STEP_KEYS


# ---- B2 — create without auth → 401 -----------------------------------


@pytest.mark.integration
async def test_b2_create_without_auth(client: AsyncClient) -> None:
    _, project_id = await _setup(client)

    r = await client.post(
        f"/api/v1/projects/{project_id}/workflow-runs",
        json={"workflow_key": _NOOP_CHAIN, "workflow_version": _WF_VERSION},
    )
    assert r.status_code == 401, r.text


# ---- B3 — create unknown project → 404 --------------------------------


@pytest.mark.integration
async def test_b3_create_unknown_project(client: AsyncClient) -> None:
    access = (await _register(client))["access_token"]

    r = await client.post(
        f"/api/v1/projects/{uuid4()}/workflow-runs",
        headers=_auth(access),
        json={"workflow_key": _NOOP_CHAIN, "workflow_version": _WF_VERSION},
    )
    assert r.status_code == 404, r.text


# ---- B4 — create unknown workflow → 422 -------------------------------


@pytest.mark.integration
async def test_b4_create_unknown_workflow(client: AsyncClient) -> None:
    access, project_id = await _setup(client)

    r = await client.post(
        f"/api/v1/projects/{project_id}/workflow-runs",
        headers=_auth(access),
        json={"workflow_key": "does-not-exist", "workflow_version": _WF_VERSION},
    )
    assert r.status_code == 422, r.text


# ---- B5 — idempotent replay: 201 then 200 -----------------------------


@pytest.mark.integration
async def test_b5_idempotent_replay(client: AsyncClient) -> None:
    access, project_id = await _setup(client)

    first = await client.post(
        f"/api/v1/projects/{project_id}/workflow-runs",
        headers=_auth(access),
        json={
            "workflow_key": _NOOP_CHAIN,
            "workflow_version": _WF_VERSION,
            "idempotency_key": "run-once",
        },
    )
    assert first.status_code == 201, first.text
    first_id = first.json()["data"]["id"]

    second = await client.post(
        f"/api/v1/projects/{project_id}/workflow-runs",
        headers=_auth(access),
        json={
            "workflow_key": _NOOP_CHAIN,
            "workflow_version": _WF_VERSION,
            "idempotency_key": "run-once",
        },
    )
    assert second.status_code == 200, second.text
    assert second.json()["data"]["id"] == first_id


# ---- B6 — create extra/invalid body → 422 -----------------------------


@pytest.mark.integration
async def test_b6_create_invalid_body(client: AsyncClient) -> None:
    access, project_id = await _setup(client)

    # Undeclared field (extra="forbid").
    extra = await client.post(
        f"/api/v1/projects/{project_id}/workflow-runs",
        headers=_auth(access),
        json={"workflow_key": _NOOP_CHAIN, "workflow_version": _WF_VERSION, "status": "running"},
    )
    assert extra.status_code == 422, extra.text

    # Missing required field.
    missing = await client.post(
        f"/api/v1/projects/{project_id}/workflow-runs",
        headers=_auth(access),
        json={"workflow_version": _WF_VERSION},
    )
    assert missing.status_code == 422, missing.text


# ---- B7 — list empty → 200 [] -----------------------------------------


@pytest.mark.integration
async def test_b7_list_empty(client: AsyncClient) -> None:
    access, project_id = await _setup(client)

    r = await client.get(f"/api/v1/projects/{project_id}/workflow-runs", headers=_auth(access))
    assert r.status_code == 200, r.text
    assert r.json()["data"] == []


# ---- B8 — list newest-first + status filter ---------------------------


@pytest.mark.integration
async def test_b8_list_and_status_filter(client: AsyncClient) -> None:
    access, project_id = await _setup(client)
    first = await _create_run(client, access, project_id)
    second = await _create_run(client, access, project_id)

    r = await client.get(f"/api/v1/projects/{project_id}/workflow-runs", headers=_auth(access))
    assert r.status_code == 200, r.text
    rows = r.json()["data"]
    assert {j["id"] for j in rows} == {first["id"], second["id"]}
    # ``created_at`` is constant within the test transaction, so the endpoint's
    # ``created_at DESC, id DESC`` order reduces to the deterministic id tiebreak.
    returned_ids = [j["id"] for j in rows]
    expected_ids = [
        j["id"] for j in sorted(rows, key=lambda j: (j["created_at"], j["id"]), reverse=True)
    ]
    assert returned_ids == expected_ids
    # Summaries carry no steps.
    assert "steps" not in rows[0]

    # Advance one to succeeded, then filter by succeeded.
    adv = await client.post(
        f"/api/v1/projects/{project_id}/workflow-runs/{first['id']}/advance",
        headers=_auth(access),
    )
    assert adv.status_code == 200, adv.text

    filtered = await client.get(
        f"/api/v1/projects/{project_id}/workflow-runs?status=succeeded", headers=_auth(access)
    )
    assert filtered.status_code == 200, filtered.text
    assert [j["id"] for j in filtered.json()["data"]] == [first["id"]]


# ---- B9 — list bad status enum → 422 ----------------------------------


@pytest.mark.integration
async def test_b9_list_bad_status_enum(client: AsyncClient) -> None:
    access, project_id = await _setup(client)

    r = await client.get(
        f"/api/v1/projects/{project_id}/workflow-runs?status=bogus", headers=_auth(access)
    )
    assert r.status_code == 422, r.text


# ---- B10 — get happy / unknown → 200 / 404 ----------------------------


@pytest.mark.integration
async def test_b10_get_happy_and_unknown(client: AsyncClient) -> None:
    access, project_id = await _setup(client)
    run = await _create_run(client, access, project_id)

    ok = await client.get(
        f"/api/v1/projects/{project_id}/workflow-runs/{run['id']}", headers=_auth(access)
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["data"]["id"] == run["id"]
    assert len(ok.json()["data"]["steps"]) == 3

    missing = await client.get(
        f"/api/v1/projects/{project_id}/workflow-runs/{uuid4()}", headers=_auth(access)
    )
    assert missing.status_code == 404, missing.text


# ---- B11 — advance noop-chain → 200 succeeded -------------------------


@pytest.mark.integration
async def test_b11_advance_noop_chain_succeeds(client: AsyncClient) -> None:
    access, project_id = await _setup(client)
    run = await _create_run(client, access, project_id, workflow_key=_NOOP_CHAIN)

    r = await client.post(
        f"/api/v1/projects/{project_id}/workflow-runs/{run['id']}/advance",
        headers=_auth(access),
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["status"] == "succeeded"
    assert data["started_at"] is not None
    assert data["finished_at"] is not None
    assert all(s["status"] == "succeeded" for s in data["steps"])
    assert data["output_summary"] == {
        "step_count": 3,
        "completed_steps": ["extract", "transform", "summarize"],
    }
    # A checkpoint per step is appended; ``created_at`` is transaction-constant and
    # checkpoint ids are random UUIDs, so the (unfiltered) latest is an id-desc
    # tiebreak — assert it resolves to one of the run's steps.
    assert data["latest_checkpoint"] is not None
    assert data["latest_checkpoint"]["step_index"] in (0, 1, 2)


# ---- B12 — advance retry-succeed → 200 succeeded, retries counted -----


@pytest.mark.integration
async def test_b12_advance_retry_succeed(client: AsyncClient) -> None:
    access, project_id = await _setup(client)
    run = await _create_run(client, access, project_id, workflow_key=_RETRY_SUCCEED)

    r = await client.post(
        f"/api/v1/projects/{project_id}/workflow-runs/{run['id']}/advance",
        headers=_auth(access),
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["status"] == "succeeded"
    flaky = next(s for s in data["steps"] if s["step_name"] == "flaky")
    assert flaky["retries"] == 2
    assert flaky["status"] == "succeeded"


# ---- B13 — advance terminal-fail → 200 failed -------------------------


@pytest.mark.integration
async def test_b13_advance_terminal_fail(client: AsyncClient) -> None:
    access, project_id = await _setup(client)
    run = await _create_run(client, access, project_id, workflow_key=_TERMINAL_FAIL)

    r = await client.post(
        f"/api/v1/projects/{project_id}/workflow-runs/{run['id']}/advance",
        headers=_auth(access),
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["status"] == "failed"
    assert data["error"]["reason"] == "terminal"
    boom = next(s for s in data["steps"] if s["step_name"] == "boom")
    assert boom["status"] == "failed"


# ---- B14 — advance already terminal → 409 -----------------------------


@pytest.mark.integration
async def test_b14_advance_already_terminal(client: AsyncClient) -> None:
    access, project_id = await _setup(client)
    run = await _create_run(client, access, project_id, workflow_key=_NOOP_CHAIN)

    first = await client.post(
        f"/api/v1/projects/{project_id}/workflow-runs/{run['id']}/advance",
        headers=_auth(access),
    )
    assert first.status_code == 200, first.text

    second = await client.post(
        f"/api/v1/projects/{project_id}/workflow-runs/{run['id']}/advance",
        headers=_auth(access),
    )
    assert second.status_code == 409, second.text


# ---- B15 — cancel queued → 200 canceled -------------------------------


@pytest.mark.integration
async def test_b15_cancel_queued(client: AsyncClient) -> None:
    access, project_id = await _setup(client)
    run = await _create_run(client, access, project_id)

    r = await client.post(
        f"/api/v1/projects/{project_id}/workflow-runs/{run['id']}/cancel",
        headers=_auth(access),
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["status"] == "canceled"
    assert data["finished_at"] is not None


# ---- B16 — cancel already canceled → 200 no-op ------------------------


@pytest.mark.integration
async def test_b16_cancel_idempotent_noop(client: AsyncClient) -> None:
    access, project_id = await _setup(client)
    run = await _create_run(client, access, project_id)

    first = await client.post(
        f"/api/v1/projects/{project_id}/workflow-runs/{run['id']}/cancel",
        headers=_auth(access),
    )
    assert first.status_code == 200, first.text

    second = await client.post(
        f"/api/v1/projects/{project_id}/workflow-runs/{run['id']}/cancel",
        headers=_auth(access),
    )
    assert second.status_code == 200, second.text
    assert second.json()["data"]["status"] == "canceled"


# ---- B17 — cancel succeeded → 409 -------------------------------------


@pytest.mark.integration
async def test_b17_cancel_succeeded(client: AsyncClient) -> None:
    access, project_id = await _setup(client)
    run = await _create_run(client, access, project_id, workflow_key=_NOOP_CHAIN)

    adv = await client.post(
        f"/api/v1/projects/{project_id}/workflow-runs/{run['id']}/advance",
        headers=_auth(access),
    )
    assert adv.status_code == 200, adv.text

    cancel = await client.post(
        f"/api/v1/projects/{project_id}/workflow-runs/{run['id']}/cancel",
        headers=_auth(access),
    )
    assert cancel.status_code == 409, cancel.text


# ---- B18 — cross-owner isolation → 404 --------------------------------


@pytest.mark.integration
async def test_b18_cross_owner_isolation(client: AsyncClient) -> None:
    owner_access, project_id = await _setup(client)
    run = await _create_run(client, owner_access, project_id)

    other_access = (await _register(client))["access_token"]

    listed = await client.get(
        f"/api/v1/projects/{project_id}/workflow-runs", headers=_auth(other_access)
    )
    assert listed.status_code == 404, listed.text

    got = await client.get(
        f"/api/v1/projects/{project_id}/workflow-runs/{run['id']}", headers=_auth(other_access)
    )
    assert got.status_code == 404, got.text

    advanced = await client.post(
        f"/api/v1/projects/{project_id}/workflow-runs/{run['id']}/advance",
        headers=_auth(other_access),
    )
    assert advanced.status_code == 404, advanced.text

    canceled = await client.post(
        f"/api/v1/projects/{project_id}/workflow-runs/{run['id']}/cancel",
        headers=_auth(other_access),
    )
    assert canceled.status_code == 404, canceled.text
