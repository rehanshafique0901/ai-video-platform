"""Unit tests for ``PublishJobCreateRequest.publish_at`` validation (α8.9b — SC3).

The creator-scheduling ingress adds an optional ``publish_at`` that must be timezone-aware and
strictly in the future, and is normalised to UTC. A rejected value raises ``ValidationError`` at
the schema boundary (which FastAPI renders as a 422). These are pure, DB-free schema tests.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.api.v1.schemas.publish_jobs import PublishJobCreateRequest

pytestmark = pytest.mark.unit


def _base(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "export_job_id": "11111111-1111-1111-1111-111111111111",
        "social_account_id": "22222222-2222-2222-2222-222222222222",
    }
    body.update(overrides)
    return body


def test_publish_at_absent_is_none() -> None:
    req = PublishJobCreateRequest.model_validate(_base())
    assert req.publish_at is None


def test_publish_at_explicit_null_is_none() -> None:
    req = PublishJobCreateRequest.model_validate(_base(publish_at=None))
    assert req.publish_at is None


def test_future_tz_aware_is_accepted() -> None:
    when = datetime.now(UTC) + timedelta(hours=2)
    req = PublishJobCreateRequest.model_validate(_base(publish_at=when.isoformat()))
    assert req.publish_at is not None
    assert req.publish_at.tzinfo is not None


def test_future_non_utc_offset_is_normalised_to_utc() -> None:
    # A future instant expressed in a +05:00 offset is accepted and stored as UTC (SC3).
    tz = timezone(timedelta(hours=5))
    when = datetime.now(tz) + timedelta(hours=3)
    req = PublishJobCreateRequest.model_validate(_base(publish_at=when.isoformat()))
    assert req.publish_at is not None
    assert req.publish_at.utcoffset() == timedelta(0)  # normalised to UTC
    assert req.publish_at == when  # same instant, different representation


def test_naive_datetime_is_rejected() -> None:
    naive = (datetime.now(UTC) + timedelta(hours=2)).replace(tzinfo=None)
    with pytest.raises(ValidationError, match="timezone-aware"):
        PublishJobCreateRequest.model_validate(_base(publish_at=naive.isoformat()))


def test_past_datetime_is_rejected() -> None:
    past = datetime.now(UTC) - timedelta(hours=1)
    with pytest.raises(ValidationError, match="future"):
        PublishJobCreateRequest.model_validate(_base(publish_at=past.isoformat()))
