"""HTTP middleware.

Currently exposes ``RequestIdMiddleware`` only. The response envelope
(API_CONTRACT §1.1) is constructed by routers and exception handlers
directly rather than by a middleware so streaming responses added in
Phase 4 don't have to fight a body-rewriting layer.
"""

from __future__ import annotations

import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

REQUEST_ID_HEADER = "X-Request-ID"


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Attach a UUIDv4 ``request_id`` to every request.

    The value comes from the incoming ``X-Request-ID`` header if
    present (so an upstream gateway can correlate), otherwise generated
    fresh. It is bound to ``structlog.contextvars`` so every log line
    emitted during the request carries it, mirrored into
    ``request.state.request_id`` for handlers, and echoed back to the
    client via the response header.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
        request.state.request_id = request_id
        structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.unbind_contextvars("request_id")
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
