"""Unit tests for ``RecordAnalyticsEvent`` (Slice α9.0 — in-memory fakes, no DB).

Prove the idempotent write half: it resolves the owner's tenant in the same UoW, records a
fresh row (``recorded``), maps the DB uniqueness refusal to an idempotent no-op
(``duplicate``), and skips cleanly when the owning user no longer exists (``skipped``). Also
proves it commits only on a real write and never on the conflict/skip paths.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace, TracebackType
from typing import Any, Self
from uuid import uuid4

import pytest

from app.application.use_cases.analytics.record_analytics_event import RecordAnalyticsEvent
from app.core.errors import ConflictError

pytestmark = pytest.mark.unit


class _FakeUsers:
    def __init__(self, user: object | None) -> None:
        self._user = user

    async def get_by_id(self, user_id: Any) -> object | None:
        return self._user


class _FakeAnalytics:
    def __init__(self, *, conflict: bool = False) -> None:
        self._conflict = conflict
        self.calls: list[dict[str, Any]] = []

    async def add(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)
        if self._conflict:
            raise ConflictError(
                "dup", details={"constraint": "uq_analytics_events_source_event_id"}
            )


class _FakeUoW:
    def __init__(self, *, user: object | None, conflict: bool = False) -> None:
        self.users = _FakeUsers(user)
        self.analytics = _FakeAnalytics(conflict=conflict)
        self.committed = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True


def _run(uow: _FakeUoW):
    use_case = RecordAnalyticsEvent(uow=uow)  # type: ignore[arg-type]
    return use_case.execute(
        user_id=uuid4(),
        event_name="publish.succeeded",
        properties={"source_event_type": "PublishJobSucceeded"},
        source_event_id=uuid4(),
        occurred_at=datetime.now(UTC),
    )


async def test_records_and_commits_with_resolved_tenant() -> None:
    tenant_id = uuid4()
    uow = _FakeUoW(user=SimpleNamespace(tenant_id=tenant_id))
    result = await _run(uow)

    assert result.status == "recorded"
    assert uow.committed is True
    (call,) = uow.analytics.calls
    assert call["tenant_id"] == tenant_id  # tenant resolved from the user (AN5)


async def test_conflict_is_idempotent_noop() -> None:
    uow = _FakeUoW(user=SimpleNamespace(tenant_id=uuid4()), conflict=True)
    result = await _run(uow)

    assert result.status == "duplicate"
    assert uow.committed is False  # the refused write never commits


async def test_unknown_user_is_skipped_without_write() -> None:
    uow = _FakeUoW(user=None)
    result = await _run(uow)

    assert result.status == "skipped"
    assert uow.analytics.calls == []  # never attempted a write
    assert uow.committed is False
