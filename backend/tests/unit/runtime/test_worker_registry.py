"""Unit tests — α9.8 worker registry (ADR-0053 PF3, PF6).

These never build a worker. ``build_registry`` only composes lazy ``run_pass`` closures, so the
whole selector and enable/disable surface can be asserted without a container, which is the point:
the decisions being tested are made at *registration*, before any worker exists.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.application.use_cases.export.export_worker import ExportPollResult
from app.application.use_cases.generation.generation_worker import GenerationPollResult
from app.application.use_cases.media.media_enrichment_worker import MediaEnrichmentPollResult
from app.application.use_cases.notifications.notification_email_worker import (
    NotificationEmailPollResult,
)
from app.application.use_cases.publishing.publish_worker import PublishPollResult
from app.application.use_cases.relay.relay_service import RelayResult
from app.application.use_cases.render.render_worker import RenderPollResult
from app.core.config import Settings
from app.runtime.worker_registry import (
    EMAIL,
    WORKER_NAMES,
    UnknownWorkerError,
    build_registry,
)

pytestmark = pytest.mark.unit


def _settings(**overrides: object) -> Settings:
    return Settings(**overrides)  # type: ignore[arg-type]


def test_default_registry_runs_every_worker() -> None:
    names = [spec.name for spec in build_registry(_settings())]

    assert names == list(WORKER_NAMES), "the default process must run every capability"


def test_a_selector_narrows_the_registry_and_preserves_order() -> None:
    specs = build_registry(_settings(), selected=frozenset({"publish", "relay"}))

    assert [spec.name for spec in specs] == ["relay", "publish"]


def test_an_unknown_worker_name_fails_before_anything_starts() -> None:
    """PF3 — a typo must not boot a healthy-looking process that silently runs nothing."""
    with pytest.raises(UnknownWorkerError) as excinfo:
        build_registry(_settings(), selected=frozenset({"relay", "pubish"}))

    message = str(excinfo.value)
    assert "pubish" in message
    assert "relay" in message, "the error should list the known workers"


def test_a_disabled_worker_is_not_registered_at_all() -> None:
    """PF6 — disabling email removes the worker, rather than leaving it to no-op each pass."""
    names = [spec.name for spec in build_registry(_settings(email_delivery_enabled=False))]

    assert EMAIL not in names
    assert len(names) == len(WORKER_NAMES) - 1


def test_selecting_a_disabled_worker_yields_an_empty_registry() -> None:
    """Explicit selection does not override the master switch; the two compose."""
    specs = build_registry(_settings(email_delivery_enabled=False), selected=frozenset({EMAIL}))

    assert specs == []


def test_long_item_workers_get_larger_drain_budgets_than_short_item_ones() -> None:
    """Budgets are sized by work-item duration — generation is paid work GEN-2 cannot retry."""
    budgets = {spec.name: spec.drain_budget for spec in build_registry(_settings())}

    assert budgets["generation"] > budgets["publish"] > budgets["relay"]
    assert budgets["relay"] == budgets["email"]


def test_every_spec_carries_the_configured_cadence() -> None:
    specs = build_registry(
        _settings(worker_poll_interval_seconds=2.5, worker_idle_ceiling_seconds=45.0)
    )

    assert {spec.interval.total_seconds() for spec in specs} == {2.5}
    assert {spec.idle_ceiling.total_seconds() for spec in specs} == {45.0}


# Idle and busy results for each of the seven real worker result types. This is the only place in
# the codebase where all seven are handled together, which is precisely what the registry is for:
# if a worker's result shape changes and its predicate is not updated, that worker silently backs
# off to its idle ceiling while its queue fills, and no other test would notice.
_FOUND_WORK_CASES: list[tuple[str, Any, Any]] = [
    (
        "relay",
        RelayResult(fetched=0, published=0, failed=0, parked=0),
        RelayResult(fetched=3, published=3, failed=0, parked=0),
    ),
    ("generation", GenerationPollResult(scanned=0, reaped=0), GenerationPollResult(scanned=1)),
    ("render", RenderPollResult(scanned=0), RenderPollResult(scanned=1)),
    ("export", ExportPollResult(scanned=0), ExportPollResult(scanned=1)),
    ("enrichment", MediaEnrichmentPollResult(scanned=0), MediaEnrichmentPollResult(scanned=1)),
    ("publish", PublishPollResult(scanned=0), PublishPollResult(scanned=1)),
    ("email", NotificationEmailPollResult(scanned=0), NotificationEmailPollResult(scanned=1)),
]


@pytest.mark.parametrize(("name", "idle", "busy"), _FOUND_WORK_CASES)
def test_found_work_reads_each_real_result_type(name: str, idle: Any, busy: Any) -> None:
    spec = next(s for s in build_registry(_settings()) if s.name == name)

    assert spec.found_work(busy) is True
    assert spec.found_work(idle) is False


def test_generation_treats_a_reaped_run_as_work() -> None:
    """A pass that terminalised an abandoned run should poll again, not start backing off."""
    spec = next(s for s in build_registry(_settings()) if s.name == "generation")

    assert spec.found_work(GenerationPollResult(scanned=0, reaped=1)) is True


def test_run_pass_is_lazy_so_registration_touches_no_container() -> None:
    """Registration must not build workers: the container is not initialised in a unit test."""
    specs = build_registry(_settings())

    # Calling any run_pass here would raise "container not initialised"; not calling them is the
    # assertion. Building the registry above already exercised the whole composition path.
    assert all(callable(spec.run_pass) for spec in specs)
