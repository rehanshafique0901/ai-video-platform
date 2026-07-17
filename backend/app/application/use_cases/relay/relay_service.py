"""``RelayService`` — the outbox relay (Slice α7.3).

Bridges the already-produced ``event_outbox`` rows (α7.1 ``RenderJob*`` / α7.2
``WorkflowRun*``) to consumers. One :meth:`relay_once` call is **one transaction
over one batch** (α7.3 sign-off Q6): claim a batch with ``FOR UPDATE SKIP
LOCKED``, publish each via the :class:`PublisherPort`, and record the outcome
(``published_at`` on success; ``attempts`` / ``last_error`` on failure), then
commit. There is **no daemon loop and no scheduler** — the α8.1 worker will call
this on a cadence; α7.3 ships the primitive (α7.3 sign-off Q1).

Delivery is **at-least-once** (ADR-0041 D9): publish precedes the ``published_at``
stamp, so a crash between the two re-delivers on a later pass; consumers dedupe on
``event.id``. A publish that raises **parks** the event in place after
``max_attempts`` (``attempts`` reaches the cap → excluded from future fetches) —
**no DLQ table, no migration** (α7.3 sign-off Q3). Every parked event emits an
``ERROR`` structured log so poison rows are observable without a monitoring
framework.

:class:`RelayResult` summarises a pass (``fetched`` / ``published`` / ``failed`` /
``parked``) so tests, logs, and later metrics are trivial without coupling to any
monitoring stack (α7.3 sign-off, added requirement).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import structlog

from app.application.interfaces.publisher import PublisherPort
from app.application.interfaces.unit_of_work import IUnitOfWork

_LOGGER = structlog.get_logger(__name__)

# α7.3 sign-off Q3 defaults. Overridable per call / via container wiring; kept as
# module constants (not a migration or schema concern) so the relay has sane
# behaviour with zero configuration.
DEFAULT_BATCH_SIZE = 100
DEFAULT_MAX_ATTEMPTS = 10


@dataclass(frozen=True, slots=True)
class RelayResult:
    """Summary of one :meth:`RelayService.relay_once` pass.

    ``fetched`` = rows claimed this pass; ``published`` = delivered + stamped;
    ``failed`` = publishes that raised (``attempts`` bumped); ``parked`` = the
    subset of ``failed`` whose bump reached ``max_attempts`` (now excluded from
    future fetches). Invariant: ``published + failed == fetched`` and
    ``parked <= failed``.
    """

    fetched: int
    published: int
    failed: int
    parked: int


class RelayService:
    """Publish a batch of pending outbox events; return a :class:`RelayResult`."""

    def __init__(
        self,
        *,
        uow: IUnitOfWork,
        publisher: PublisherPort,
        batch_size: int = DEFAULT_BATCH_SIZE,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self._uow = uow
        self._publisher = publisher
        self._batch_size = batch_size
        self._max_attempts = max_attempts

    async def relay_once(self, *, batch_size: int | None = None) -> RelayResult:
        """Run a single relay pass over one batch, in one transaction."""
        limit = batch_size if batch_size is not None else self._batch_size
        published = 0
        failed = 0
        parked = 0

        async with self._uow:
            events = await self._uow.outbox.fetch_unpublished(
                limit=limit, max_attempts=self._max_attempts
            )
            for event in events:
                try:
                    await self._publisher.publish(event)
                except Exception as exc:
                    failed += 1
                    await self._uow.outbox.mark_failed(event_id=event.id, error=_format_error(exc))
                    # ``attempts`` in the snapshot is the count of PRIOR failures;
                    # this failure makes it ``attempts + 1``. When that reaches the
                    # cap the row is parked (fetch_unpublished excludes it).
                    new_attempts = event.attempts + 1
                    is_parked = new_attempts >= self._max_attempts
                    if is_parked:
                        parked += 1
                    _LOGGER.error(
                        "outbox.publish_failed",
                        event_id=str(event.id),
                        aggregate_id=str(event.aggregate_id),
                        aggregate_type=event.aggregate_type,
                        event_type=event.event_type,
                        attempts=new_attempts,
                        max_attempts=self._max_attempts,
                        parked=is_parked,
                        exception_type=type(exc).__name__,
                        exception_message=str(exc),
                    )
                else:
                    published += 1
                    await self._uow.outbox.mark_published(
                        event_id=event.id, published_at=datetime.now(UTC)
                    )
            await self._uow.commit()

        result = RelayResult(fetched=len(events), published=published, failed=failed, parked=parked)
        _LOGGER.info(
            "outbox.relay_pass",
            fetched=result.fetched,
            published=result.published,
            failed=result.failed,
            parked=result.parked,
            batch_size=limit,
        )
        return result


def _format_error(exc: Exception) -> str:
    """Compact ``last_error`` text: ``ExceptionType: message`` (truncated)."""
    text = f"{type(exc).__name__}: {exc}"
    return text if len(text) <= 1000 else text[:997] + "..."
