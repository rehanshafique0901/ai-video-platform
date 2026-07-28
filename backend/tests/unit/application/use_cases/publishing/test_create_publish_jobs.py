"""Unit tests for ``CreatePublishJobs`` — the α9.4 multi-destination fan-out.

Proves the orchestration layer only: it composes ``CreatePublishJob`` (a fake here) and splits
failures into **shared prerequisite** (abort the whole request) vs **per-account** (recorded as
an item, others proceed). It never re-implements validation, idempotency, or persistence.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.application.use_cases.publishing.create_publish_job import CreatePublishJobResult
from app.application.use_cases.publishing.create_publish_jobs import CreatePublishJobs
from app.core.errors import NotFoundError, ValidationFailedError

_TENANT = uuid4()
_USER = uuid4()
_EXPORT = uuid4()

pytestmark = pytest.mark.unit


class _FakeCreateOne:
    """Scripted stand-in for ``CreatePublishJob`` keyed by ``social_account_id``.

    Each account maps to either a ``CreatePublishJobResult`` (success/replay) or an exception to
    raise. Records the accounts it was invoked with (in order) to prove pure delegation.
    """

    def __init__(self, script: dict[UUID, object]) -> None:
        self._script = script
        self.calls: list[UUID] = []

    async def execute(self, *, social_account_id: UUID, **_: object) -> CreatePublishJobResult:
        self.calls.append(social_account_id)
        outcome = self._script[social_account_id]
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, CreatePublishJobResult)
        return outcome


def _ok(*, created: bool) -> CreatePublishJobResult:
    # The fan-out only stores/inspects ``.job``/``.created`` — a sentinel job suffices.
    return CreatePublishJobResult(job=SimpleNamespace(id=uuid4()), created=created)  # type: ignore[arg-type]


async def _run(create_one: _FakeCreateOne, account_ids: list[UUID]):
    use_case = CreatePublishJobs(create_one=create_one)  # type: ignore[arg-type]
    return await use_case.execute(
        owner_user_id=_USER,
        tenant_id=_TENANT,
        export_job_id=_EXPORT,
        social_account_ids=account_ids,
    )


async def test_all_accounts_created_preserves_order() -> None:
    a, b, c = uuid4(), uuid4(), uuid4()
    create_one = _FakeCreateOne({a: _ok(created=True), b: _ok(created=True), c: _ok(created=True)})

    result = await _run(create_one, [a, b, c])

    assert [i.social_account_id for i in result.items] == [a, b, c]
    assert all(i.created and i.job is not None and i.error is None for i in result.items)
    assert create_one.calls == [a, b, c]


async def test_mixed_created_and_idempotent_replay() -> None:
    a, b = uuid4(), uuid4()
    create_one = _FakeCreateOne({a: _ok(created=True), b: _ok(created=False)})

    result = await _run(create_one, [a, b])

    assert result.items[0].created is True
    # Replay: not created, but the existing job is still returned (unambiguous outcome).
    assert result.items[1].created is False
    assert result.items[1].job is not None
    assert result.items[1].error is None


async def test_per_account_not_found_is_recorded_others_proceed() -> None:
    good, bad = uuid4(), uuid4()
    create_one = _FakeCreateOne(
        {
            good: _ok(created=True),
            bad: NotFoundError("social account not found", details={"social_account_id": str(bad)}),
        }
    )

    result = await _run(create_one, [good, bad])

    assert result.items[0].created is True and result.items[0].error is None
    assert result.items[1].created is False
    assert result.items[1].job is None
    assert result.items[1].error is not None
    assert result.items[1].error.code == "NOT_FOUND"


async def test_per_account_not_connected_and_unsupported_are_isolated() -> None:
    not_connected, unsupported, good = uuid4(), uuid4(), uuid4()
    create_one = _FakeCreateOne(
        {
            not_connected: ValidationFailedError(
                "social account is not connected",
                details={"social_account_id": str(not_connected), "status": "revoked"},
            ),
            unsupported: ValidationFailedError(
                "no destination adapter is registered for this platform",
                details={"platform": "tiktok"},
            ),
            good: _ok(created=True),
        }
    )

    result = await _run(create_one, [not_connected, unsupported, good])

    assert result.items[0].error is not None and result.items[0].error.code == "VALIDATION_FAILED"
    assert result.items[1].error is not None and result.items[1].error.code == "VALIDATION_FAILED"
    assert result.items[2].created is True and result.items[2].error is None


async def test_shared_export_not_found_aborts_whole_request() -> None:
    a, b = uuid4(), uuid4()
    create_one = _FakeCreateOne(
        {a: NotFoundError("export job not found", details={"export_job_id": str(_EXPORT)})}
    )

    with pytest.raises(NotFoundError):
        await _run(create_one, [a, b])

    # Fail-fast: aborted on the first account; the second was never attempted.
    assert create_one.calls == [a]


async def test_shared_export_not_ready_aborts_whole_request() -> None:
    a = uuid4()
    create_one = _FakeCreateOne(
        {
            a: ValidationFailedError(
                "export job has no completed delivery artifact to publish",
                details={"export_job_id": str(_EXPORT), "status": "running"},
            )
        }
    )

    with pytest.raises(ValidationFailedError):
        await _run(create_one, [a])


async def test_shared_thumbnail_failure_aborts_whole_request() -> None:
    a = uuid4()
    thumb = uuid4()
    create_one = _FakeCreateOne(
        {
            a: ValidationFailedError(
                "thumbnail media asset must be an image",
                details={"thumbnail_media_asset_id": str(thumb), "kind": "video"},
            )
        }
    )

    with pytest.raises(ValidationFailedError):
        await _run(create_one, [a])


async def test_per_account_failure_before_a_shared_failure_still_aborts() -> None:
    # account-1 fails its own check (per-account); account-2 surfaces a shared export error →
    # the whole request aborts (shared wins), and no job was created.
    bad_account, valid_account = uuid4(), uuid4()
    create_one = _FakeCreateOne(
        {
            bad_account: NotFoundError(
                "social account not found", details={"social_account_id": str(bad_account)}
            ),
            valid_account: NotFoundError(
                "export job not found", details={"export_job_id": str(_EXPORT)}
            ),
        }
    )

    with pytest.raises(NotFoundError):
        await _run(create_one, [bad_account, valid_account])

    assert create_one.calls == [bad_account, valid_account]
