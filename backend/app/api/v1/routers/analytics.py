"""``/api/v1/analytics/*`` router (α9.0 — Creator Analytics Foundation).

A single authenticated, **read-only** endpoint that surfaces the caller's analytics as
per-``event_name`` counts over a time window. The router stays thin: it resolves the optional
window (defaulting to a trailing 30 days), delegates aggregation to ``GetCreatorAnalytics``,
and projects the result through the standard ``envelope``. All scope comes from
:data:`CurrentUserDep`.

* ``GET /analytics/summary?since=&until=`` → 200, the owner-scoped counts + total.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app.api.v1.deps import CreatorAnalyticsDep, CurrentUserDep
from app.api.v1.helpers import envelope
from app.api.v1.schemas.analytics import AnalyticsSummaryPublic, AnalyticsSummaryQuery

router = APIRouter(prefix="/analytics", tags=["analytics"])

# The default analytics window when the caller omits bounds (AN8): trailing 30 days.
_DEFAULT_WINDOW = timedelta(days=30)


@router.get("/summary")
async def analytics_summary(
    request: Request,
    current_user: CurrentUserDep,
    use_case: CreatorAnalyticsDep,
    query: Annotated[AnalyticsSummaryQuery, Query()],
) -> JSONResponse:
    """Return the authenticated caller's read-only analytics summary over ``[since, until)``.

    Owner-scoped: only the caller's own recorded events are counted (a fresh caller sees
    all-zero). Absent ``until`` defaults to now; absent ``since`` defaults to 30 days before
    ``until``. ``401`` if unauthenticated; ``422`` for a naive or mis-ordered window.
    """
    until = query.until or datetime.now(UTC)
    since = query.since or (until - _DEFAULT_WINDOW)
    summary = await use_case.execute(
        tenant_id=current_user.tenant_id,
        owner_user_id=current_user.id,
        since=since,
        until=until,
    )
    return JSONResponse(content=envelope(AnalyticsSummaryPublic.from_domain(summary), request))
