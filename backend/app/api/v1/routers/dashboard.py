"""``/api/v1/dashboard/*`` router (α8.9c — Creator Dashboard).

A single authenticated, **read-only** endpoint that surfaces the caller's product state as
scalar counts. The router stays thin: DTO projection + ``envelope``; all scope comes from
:data:`CurrentUserDep`, and the aggregation lives in the ``GetCreatorDashboard`` use case.

* ``GET /dashboard/summary`` → 200, the owner-scoped summary (publish-job counts by status,
  connected/total social accounts, unread notification count, media total).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.api.v1.deps import CreatorDashboardDep, CurrentUserDep
from app.api.v1.helpers import envelope
from app.api.v1.schemas.dashboard import DashboardSummaryPublic

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/summary")
async def dashboard_summary(
    request: Request,
    current_user: CurrentUserDep,
    use_case: CreatorDashboardDep,
) -> JSONResponse:
    """Return the authenticated caller's read-only dashboard summary.

    Owner-scoped: only the caller's own publish jobs, social accounts, notifications, and
    media are counted (a fresh caller sees all-zero). ``401`` if unauthenticated.
    """
    summary = await use_case.execute(
        tenant_id=current_user.tenant_id,
        owner_user_id=current_user.id,
    )
    return JSONResponse(content=envelope(DashboardSummaryPublic.from_domain(summary), request))
