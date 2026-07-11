"""Unit tests for the cursor (keyset) pagination primitives (Slice α5a).

Coverage map (α5a pre-flight §8):

* P1 — encode → decode round-trips a ``(created_at, id)`` position
  exactly (microsecond + tz preserved).
* P2 — the token is opaque base64url (no raw timestamp/UUID leaks in a
  form a client could rely on) and URL-safe.
* P3 — every malformed-token class decodes to ``ValidationFailedError``
  (422), never a 500 and never a silent wrong position: garbage,
  valid-base64-non-JSON, wrong schema version, missing key, and an
  unparseable UUID.
* P4 — :class:`Page` carries items + ``next_cursor`` verbatim.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.application.pagination import (
    Cursor,
    Page,
    decode_cursor,
    encode_cursor,
)
from app.core.errors import ValidationFailedError


@pytest.mark.unit
def test_p1_encode_decode_round_trip() -> None:
    original = Cursor(
        created_at=datetime(2026, 3, 4, 5, 6, 7, 890123, tzinfo=UTC),
        id=uuid4(),
    )
    token = encode_cursor(original)
    restored = decode_cursor(token)
    assert restored.created_at == original.created_at
    assert restored.id == original.id


@pytest.mark.unit
def test_p2_token_is_urlsafe_base64() -> None:
    token = encode_cursor(Cursor(created_at=datetime.now(UTC), id=uuid4()))
    # URL-safe alphabet only — no '+' or '/' that would need escaping in
    # a query string.
    assert "+" not in token and "/" not in token
    # Round-trips through urlsafe base64 (i.e. it *is* valid base64url).
    base64.urlsafe_b64decode(token.encode("ascii"))


def _b64(payload: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode("ascii")


@pytest.mark.unit
@pytest.mark.parametrize(
    "kind",
    ["bad_base64", "not_json", "wrong_version", "missing_key", "bad_uuid"],
)
def test_p3_malformed_tokens_raise_validation_failed(kind: str) -> None:
    """Every malformed-token class collapses to a 422, never a 500."""
    tokens = {
        # Invalid base64 alphabet / padding.
        "bad_base64": "not-base64-!!!",
        # Valid base64 whose body is not JSON.
        "not_json": base64.urlsafe_b64encode(b"not json at all").decode("ascii"),
        # Valid JSON, unsupported schema version.
        "wrong_version": _b64({"v": 999}),
        # Correct version but missing the created_at key.
        "missing_key": _b64({"v": 1, "id": str(uuid4())}),
        # Correct shape but an unparseable UUID.
        "bad_uuid": _b64({"v": 1, "created_at": "2026-01-01T00:00:00+00:00", "id": "nope"}),
    }
    with pytest.raises(ValidationFailedError):
        decode_cursor(tokens[kind])


@pytest.mark.unit
def test_p4_page_holds_items_and_cursor() -> None:
    page: Page[int] = Page(items=[1, 2, 3], next_cursor="abc")
    assert page.items == [1, 2, 3]
    assert page.next_cursor == "abc"

    last: Page[int] = Page(items=[], next_cursor=None)
    assert last.items == []
    assert last.next_cursor is None
