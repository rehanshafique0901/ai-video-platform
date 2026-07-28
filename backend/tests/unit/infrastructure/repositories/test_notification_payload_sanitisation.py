"""Unit tests for the centralised notification payload sanitisation (α9.5, ADR-0051).

Reserved ``_``-prefixed bookkeeping keys (e.g. the ``_email`` email-delivery state) are an
implementation detail and must never reach the read model / public API. Sanitisation is centralised
in the single repository row→entity boundary; this pins the pure helper it delegates to.
"""

from __future__ import annotations

import pytest

from app.infrastructure.repositories.notification_repository import _public_payload

pytestmark = pytest.mark.unit


def test_strips_reserved_email_namespace() -> None:
    raw = {
        "platform": "youtube",
        "post_url": "https://youtu.be/x",
        "_email": {"attempts": 2, "state": "pending", "last_error": "smtp_451"},
    }
    assert _public_payload(raw) == {
        "platform": "youtube",
        "post_url": "https://youtu.be/x",
    }


def test_strips_any_underscore_prefixed_key() -> None:
    # The convention is prefix-based, so any future internal namespace is hidden too.
    raw = {"kept": 1, "_internal": 2, "_email": {"x": 1}}
    assert _public_payload(raw) == {"kept": 1}


def test_public_only_payload_is_unchanged() -> None:
    raw = {"export_job_id": "abc", "status": "succeeded"}
    assert _public_payload(raw) == raw


def test_empty_payload() -> None:
    assert _public_payload({}) == {}
