"""Health and readiness endpoints (public).

Both endpoints are documented as public in ``API_CONTRACT.md`` §2.
``/healthz`` is process-liveness only; ``/readyz`` confirms the
database is reachable via a ``SELECT 1``.

Success responses follow the envelope in ``API_CONTRACT.md`` §1.1:
``{"data": ..., "meta": {"request_id": "..."}}``.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.v1.deps import SessionDep
from app.api.v1.helpers import envelope

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz(request: Request) -> JSONResponse:
    """Process liveness — 200 whenever the process can respond."""
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=envelope({"status": "ok"}, request),
    )


@router.get("/readyz")
async def readyz(request: Request, session: SessionDep) -> JSONResponse:
    """Readiness — 200 if ``SELECT 1`` succeeds, 503 otherwise."""
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=envelope({"status": "db_unreachable"}, request),
        )
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=envelope({"status": "ready"}, request),
    )
