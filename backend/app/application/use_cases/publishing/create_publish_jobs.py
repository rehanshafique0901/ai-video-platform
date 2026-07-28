"""``CreatePublishJobs`` — multi-destination publish fan-out (α9.4).

Contract:

    POST /api/v1/publish-jobs/batch
      body:  { export_job_id, social_account_ids[1..20], title?, description?, tags?,
               visibility?, publish_at?, thumbnail_media_asset_id? }
      → 201  { data: [ { social_account_id, created, publish_job?, error? }, … ], meta }
      → 404  { error: NOT_FOUND }          (a *shared* prerequisite: export/thumbnail not owned)
      → 422  { error: VALIDATION_FAILED }  (a *shared* prerequisite: export not ready /
                                            thumbnail not an image / bad body / dup ids)
      → 401  { error: UNAUTHENTICATED }    (via CurrentUserDep)

**Orchestration only.** This use case composes the existing :class:`CreatePublishJob` once per
account — it duplicates **no** validation, idempotency, or persistence logic (that use case stays
the single source of publishing truth). Its only job is to split failures:

* **Shared prerequisite failure** (the export, or the optional thumbnail) → the request is aborted
  (fail-fast ``404``/``422``). Such a failure is invalid for *every* account, so aborting is
  correct; and it can never discard an already-created job — a created job proves the shared inputs
  already passed (``CreatePublishJob`` validates account → export → thumbnail, then inserts).
* **Per-account failure** (account not owned / not connected / unsupported platform) → recorded as
  that item's outcome so one bad account never blocks the rest (best-effort fan-out).

Idempotency (PUB-7) is unchanged: each account independently replays its own existing job
(``created=False``). Scheduling (α8.9b), captions (α9.1), thumbnails (α9.3), notifications (α8.9a),
and analytics (α9.0) all keep working automatically because they attach to the individual
``PublishJob``s this use case creates — there is no batch-specific runtime concept.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import structlog

from app.application.use_cases.publishing.create_publish_job import CreatePublishJob
from app.core.errors import ApplicationError, NotFoundError, ValidationFailedError
from app.domain.publishing.content_package import Visibility
from app.domain.publishing.publish_job import PublishJob

_LOGGER = structlog.get_logger(__name__)

# Error-detail keys that classify a caught ``CreatePublishJob`` failure. These mirror the
# ``details`` that use case already sets (create_publish_job.py) — the single validation source.
# A *shared* key aborts the whole request; a *per-account* key is recorded as an item outcome.
_SHARED_DETAIL_KEYS = frozenset({"export_job_id", "thumbnail_media_asset_id"})
_PER_ACCOUNT_DETAIL_KEYS = frozenset({"social_account_id", "platform"})


@dataclass(frozen=True, slots=True)
class FanOutError:
    """A neutral, per-account failure (never a credential/platform internal)."""

    code: str
    message: str


@dataclass(frozen=True, slots=True)
class PublishFanOutItem:
    """One account's fan-out outcome — exactly one of ``job`` or ``error`` is set."""

    social_account_id: UUID
    created: bool
    job: PublishJob | None
    error: FanOutError | None


@dataclass(frozen=True, slots=True)
class CreatePublishJobsResult:
    """The per-account outcomes, in the caller's requested order."""

    items: tuple[PublishFanOutItem, ...]


class CreatePublishJobs:
    """Fan out one export publish to N connected accounts (composes :class:`CreatePublishJob`)."""

    def __init__(self, create_one: CreatePublishJob) -> None:
        self._create_one = create_one

    async def execute(
        self,
        *,
        owner_user_id: UUID,
        tenant_id: UUID,
        export_job_id: UUID,
        social_account_ids: Sequence[UUID],
        title: str | None = None,
        description: str | None = None,
        tags: tuple[str, ...] | None = None,
        visibility: Visibility | None = None,
        publish_at: datetime | None = None,
        thumbnail_media_asset_id: UUID | None = None,
        ip: str | None = None,
    ) -> CreatePublishJobsResult:
        items: list[PublishFanOutItem] = []
        for social_account_id in social_account_ids:
            try:
                result = await self._create_one.execute(
                    owner_user_id=owner_user_id,
                    tenant_id=tenant_id,
                    export_job_id=export_job_id,
                    social_account_id=social_account_id,
                    title=title,
                    description=description,
                    tags=tags,
                    visibility=visibility,
                    publish_at=publish_at,
                    thumbnail_media_asset_id=thumbnail_media_asset_id,
                    ip=ip,
                )
            except (NotFoundError, ValidationFailedError) as exc:
                if self._is_shared_failure(exc):
                    # Invalid for every account → abort the whole request (fail-fast). Cannot
                    # discard a created job: a created job proves the shared inputs already passed.
                    raise
                items.append(
                    PublishFanOutItem(
                        social_account_id=social_account_id,
                        created=False,
                        job=None,
                        error=FanOutError(code=exc.code, message=exc.message),
                    )
                )
                continue
            items.append(
                PublishFanOutItem(
                    social_account_id=social_account_id,
                    created=result.created,
                    job=result.job,
                    error=None,
                )
            )

        _LOGGER.info(
            "publish_jobs.fan_out",
            export_job_id=str(export_job_id),
            owner_user_id=str(owner_user_id),
            requested=len(social_account_ids),
            created=sum(1 for i in items if i.created),
            replayed=sum(1 for i in items if i.job is not None and not i.created),
            failed=sum(1 for i in items if i.error is not None),
            ip=ip,
        )
        return CreatePublishJobsResult(items=tuple(items))

    @staticmethod
    def _is_shared_failure(exc: ApplicationError) -> bool:
        """Classify a caught create failure: shared prerequisite (abort) vs per-account (record)."""
        keys = set(exc.details)
        if keys & _SHARED_DETAIL_KEYS:
            return True
        # A per-account key → recorded as an item (False). An unknown shape → fail safe by
        # aborting (True) rather than silently masking an error.
        return not (keys & _PER_ACCOUNT_DETAIL_KEYS)
