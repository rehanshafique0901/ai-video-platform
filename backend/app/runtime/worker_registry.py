"""The worker registry — the composition root that tells the host what to run (α9.8, ADR-0053).

This is deliberately **the only type-aware component** in ``app.runtime`` (PF2). The supervisor
handles ``WorkerSpec[Any]``; every statement that knows a ``RelayResult`` has ``fetched`` while a
``PublishPollResult`` has ``scanned`` lives here, inside a ``found_work`` predicate. Keeping that
knowledge in one file is what lets the host be tested without a container, a database, or a single
real worker.

Three registration rules follow from ADR-0053, all applied here at **registration** time and never
per pass (HOST-1 — the set is fixed for the process lifetime):

* **A disabled worker is not registered.** ``email_delivery_enabled`` decides whether the email
  worker exists in this process, not whether each of its passes no-ops. A worker that runs in order
  to do nothing is a worker whose logs, liveness marker, and pass counters all lie about it.
* **A selected worker must exist.** An unrecognised name fails startup, because the alternative
  failure is the worst kind: a process that boots healthy while a capability silently never runs —
  precisely the state this slice exists to end.
* **Every worker is built fresh per pass.** ``run_pass`` calls the container factory each time, so a
  pass gets a new unit of work and nothing leaks between passes.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from datetime import timedelta
from typing import Any

from app.core import container
from app.core.config import Settings
from app.runtime.worker_host import WorkerSpec

# Registration order is task-creation order. It carries no semantic weight — HOST-2 guarantees the
# workers do not interact — but keeping it stable makes startup logs comparable between deploys.
RELAY = "relay"
GENERATION = "generation"
RENDER = "render"
EXPORT = "export"
ENRICHMENT = "enrichment"
PUBLISH = "publish"
EMAIL = "email"

WORKER_NAMES: tuple[str, ...] = (RELAY, GENERATION, RENDER, EXPORT, ENRICHMENT, PUBLISH, EMAIL)


class UnknownWorkerError(ValueError):
    """Raised when a selector names a worker that does not exist. Fails the process at startup."""


def build_registry(
    settings: Settings, *, selected: frozenset[str] | None = None
) -> list[WorkerSpec[Any]]:
    """Build the specs for this process. ``selected=None`` means every enabled worker.

    Raises :class:`UnknownWorkerError` before any worker starts if the selector names a worker that
    does not exist.
    """
    if selected is not None:
        unknown = sorted(selected - set(WORKER_NAMES))
        if unknown:
            raise UnknownWorkerError(
                f"unknown worker(s): {', '.join(unknown)}. "
                f"Known workers: {', '.join(WORKER_NAMES)}"
            )

    interval = timedelta(seconds=settings.worker_poll_interval_seconds)
    idle_ceiling = timedelta(seconds=settings.worker_idle_ceiling_seconds)
    default_drain = timedelta(seconds=settings.worker_drain_budget_seconds)
    media_drain = timedelta(seconds=settings.worker_media_drain_budget_seconds)

    def spec[
        T
    ](
        name: str,
        run_pass: Callable[[], Coroutine[Any, Any, T]],
        found_work: Callable[[T], bool],
        drain_budget: timedelta,
    ) -> WorkerSpec[T]:
        return WorkerSpec(
            name=name,
            run_pass=run_pass,
            found_work=found_work,
            interval=interval,
            idle_ceiling=idle_ceiling,
            drain_budget=drain_budget,
        )

    specs: list[WorkerSpec[Any]] = [
        # PF7 — the relay keeps its own ``DEFAULT_BATCH_SIZE``. Outbox rows are sub-second, so the
        # scan dominates and batching is right (PF4); ``relay_once`` already accepts a per-call
        # override should that ever change, so the frozen module stays shut either way.
        spec(
            RELAY,
            lambda: container.get_relay_service().relay_once(),
            lambda result: result.fetched > 0,
            default_drain,
        ),
        spec(
            GENERATION,
            lambda: container.get_generation_worker().run_once(),
            # Reaping counts as work: a pass that terminalised an abandoned run should poll again
            # promptly rather than start backing off as though the queue were empty.
            lambda result: result.scanned > 0 or result.reaped > 0,
            timedelta(seconds=settings.worker_generation_drain_budget_seconds),
        ),
        spec(
            RENDER,
            lambda: container.get_render_worker().run_once(),
            lambda result: result.scanned > 0,
            media_drain,
        ),
        spec(
            EXPORT,
            lambda: container.get_export_worker().run_once(),
            lambda result: result.scanned > 0,
            media_drain,
        ),
        spec(
            ENRICHMENT,
            lambda: container.get_media_enrichment_worker().run_once(),
            lambda result: result.scanned > 0,
            media_drain,
        ),
        spec(
            PUBLISH,
            lambda: container.get_publish_worker().run_once(),
            lambda result: result.scanned > 0,
            timedelta(seconds=settings.worker_publish_drain_budget_seconds),
        ),
        spec(
            EMAIL,
            lambda: container.get_notification_email_worker().run_once(),
            lambda result: result.scanned > 0,
            default_drain,
        ),
    ]

    enabled: dict[str, bool] = {EMAIL: settings.email_delivery_enabled}
    return [
        s for s in specs if enabled.get(s.name, True) and (selected is None or s.name in selected)
    ]
