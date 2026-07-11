"""Cursor (keyset) pagination primitives for list use cases.

α5a introduces the first paginated read (``GET /projects``). The
API_CONTRACT (§1.1 / §6) pins **cursor-based** pagination: an opaque
``?cursor=`` token plus ``meta.next_cursor``. These primitives live in
the **application** layer (not ``app.api``) because the list use case
owns pagination semantics — it decodes the incoming cursor, asks the
repository for ``limit + 1`` rows to detect a further page, trims to
``limit``, and encodes the next-page cursor from the last row. The API
layer only forwards the opaque token in and the ``next_cursor`` out.
(The import-linter contract forbids ``app.application`` from importing
``app.api``, so this cannot live under the router.)

Keyset (not offset) pagination is used so pages stay stable under
concurrent inserts: the cursor pins the last-seen ``(created_at, id)``
ordering key rather than a numeric offset that drifts as rows are
added or soft-deleted. See the α5a pre-flight D6 / D14 and
``docs/domain/PROJECT_AGGREGATE.md`` §3.

The token is a base64url-encoded JSON blob carrying a schema-version
field (``v``) for forward compatibility. It is **opaque** to clients —
they must treat it as a bearer token, never parse it. A malformed or
tampered token yields :class:`~app.core.errors.ValidationFailedError`
(422), never a 500.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Generic, TypeVar
from uuid import UUID

from app.core.errors import ValidationFailedError

# Bump when the cursor payload shape changes. Old tokens then decode to
# a clean 422 ("invalid cursor") rather than silently mis-paginating.
_CURSOR_VERSION = 1

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Cursor:
    """The keyset position: the ordering tuple of the last returned row.

    ``(created_at, id)`` is a *total* order (see α5a D14): ``created_at``
    alone is not unique, so ``id`` breaks ties deterministically and
    guarantees no row is duplicated or skipped across page boundaries.
    """

    created_at: datetime
    id: UUID


@dataclass(frozen=True, slots=True)
class Page(Generic[T]):
    """A single page of results plus the cursor for the next page.

    ``next_cursor`` is ``None`` iff this is the last page — the client
    stops paginating when it is absent from ``meta``.
    """

    items: list[T]
    next_cursor: str | None


def encode_cursor(cursor: Cursor) -> str:
    """Serialise a :class:`Cursor` into an opaque base64url token."""
    payload = {
        "v": _CURSOR_VERSION,
        "created_at": cursor.created_at.isoformat(),
        "id": str(cursor.id),
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(token: str) -> Cursor:
    """Parse an opaque token back into a :class:`Cursor`.

    Raises :class:`~app.core.errors.ValidationFailedError` (422) for any
    malformed / tampered / stale-schema token. Every failure mode
    (bad base64 padding, non-JSON body, wrong schema version, missing
    key, unparseable timestamp/UUID) collapses into the same 422 so a
    client cannot probe the cursor internals — and, critically, so a
    fuzzed ``?cursor=`` never surfaces as an unhandled 500.
    """
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get("v") != _CURSOR_VERSION:
            raise ValueError("unsupported or missing cursor version")
        return Cursor(
            created_at=datetime.fromisoformat(payload["created_at"]),
            id=UUID(payload["id"]),
        )
    except (ValueError, KeyError, TypeError) as exc:
        # ``binascii.Error`` (bad base64) and ``json.JSONDecodeError``
        # are both ``ValueError`` subclasses, as are ``UUID(...)`` and
        # ``datetime.fromisoformat(...)`` failures — one clause covers
        # every decode fault.
        raise ValidationFailedError(
            "invalid pagination cursor",
            details={"field": "cursor"},
        ) from exc
