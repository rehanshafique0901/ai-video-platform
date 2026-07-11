"""Shared HTTP helpers for the v1 API routers.

Centralizes two concerns that were previously copy-pasted across every
router (α2/α3/α4/α5a), so each new endpoint reuses one implementation
instead of adding another near-identical copy:

* :func:`client_ip` — best-effort caller IP for audit logs (honours the
  first ``X-Forwarded-For`` hop, falls back to the socket peer).
* :func:`meta` / :func:`envelope` — the API_CONTRACT §1.1 **success**
  envelope: ``{ "data": ..., "meta": { "request_id", "next_cursor"? } }``.
  ``next_cursor`` is emitted only when supplied (its absence is the
  "last page" signal per API_CONTRACT §6).

The **error** envelope is intentionally NOT here — it lives in
``app.core.errors`` and is produced by the centralized exception
handlers, a separate concern from the success path.

This module imports only ``fastapi`` + ``pydantic`` (no ``app.*``), so
``deps.py`` and every router can import it without any risk of an
import cycle. It sits in the API layer and is only ever imported by API
code, preserving the layering contracts.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from pydantic import BaseModel


def client_ip(request: Request) -> str | None:
    """Best-effort caller IP for audit logs.

    Prefers the first ``X-Forwarded-For`` hop (the original client when
    behind a proxy/load-balancer), falling back to the direct socket
    peer. Returns ``None`` when neither is available.
    """
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip() or None
    return request.client.host if request.client else None


def meta(request: Request, *, next_cursor: str | None = None) -> dict[str, Any]:
    """Build the API_CONTRACT §1.1 ``meta`` block.

    Always carries ``request_id`` (from the request-id middleware);
    includes ``next_cursor`` only when provided, so a ``None`` cursor
    means "last page" by omission.
    """
    block: dict[str, Any] = {"request_id": getattr(request.state, "request_id", "")}
    if next_cursor is not None:
        block["next_cursor"] = next_cursor
    return block


def _dump(data: Any) -> Any:
    """JSON-normalize response data: Pydantic models (and lists thereof)
    are dumped with ``mode="json"``; anything else passes through."""
    if isinstance(data, BaseModel):
        return data.model_dump(mode="json")
    if isinstance(data, list):
        return [_dump(item) for item in data]
    return data


def envelope(data: Any, request: Request, *, next_cursor: str | None = None) -> dict[str, Any]:
    """Wrap ``data`` in the standard success envelope.

    ``data`` may be a Pydantic model, a list of models, or an
    already-JSON-serializable object (dict/list/primitive) — models are
    serialized via :func:`_dump`. Use ``next_cursor`` for paginated list
    responses.
    """
    return {"data": _dump(data), "meta": meta(request, next_cursor=next_cursor)}
