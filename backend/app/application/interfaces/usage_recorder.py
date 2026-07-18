"""Port + DTOs: the Usage Recorder seam (Slice α7.5).

Turns one **terminal** provider call into exactly one immutable, priced
``usage_records`` row (ADR-0019), idempotent on ``request_id`` (ADR-0033), priced
against ``ai_model_pricing`` (CR-11). This is ADR-0041 **D13**'s usage half; the
``credit_ledger`` half is a later slice (α7.5 sign-off Q1).

Per the α7.5 sign-off:

* **Q2 (wiring):** the recorder is shipped as an explicit **seam** — the α7.6
  pipeline calls :meth:`UsageRecorderPort.record` around the dispatch. Nothing is
  wired into the runner/dispatcher this slice.
* **Q3 (command):** :class:`RecordUsageCommand` is the **application contract, not
  the ORM** — it carries context (e.g. ``render_job_id``) that has no
  ``usage_records`` column, so future slices need no breaking change.
* **W7.5.1 (observational):** the recorder's only write is ``usage_records`` — it
  never mutates an aggregate. That is why the repository ports here are limited to
  usage + pricing.

The DTOs are **neutral** (no SQLAlchemy import): the repositories map them to/from
the ORM, exactly as ``OutboxEvent`` (in :mod:`app.application.interfaces.publisher`)
is the neutral read-model for ``event_outbox``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from app.application.interfaces.providers import Capability, ProviderStatus, ProviderUsage


class PricingUnit(StrEnum):
    """The billable units, mirroring the DB ``pricing_unit`` enum (schema §0.1).

    Kept as an application-level vocabulary so the accounting policy and the
    ``usage_records.unit`` / ``ai_model_pricing.unit`` columns speak the same
    strings without importing the SQLAlchemy enum. ``embedding`` /  ``megapixel``
    exist in the DB enum but no α7.5 capability maps to them yet.
    """

    PROMPT_TOKEN = "prompt_token"
    COMPLETION_TOKEN = "completion_token"
    IMAGE = "image"
    MEGAPIXEL = "megapixel"
    VIDEO_SECOND = "video_second"
    AUDIO_SECOND = "audio_second"
    EMBEDDING = "embedding"


class UsageStatus(StrEnum):
    """The terminal usage outcomes α7.5 writes (DB ``usage_status`` enum, schema §0.1).

    α7.5 maps ``ProviderStatus.SUCCEEDED → SUCCESS`` and ``FAILED → FAILED`` (Q6);
    ``PARTIAL`` / ``TIMEOUT`` are valid enum values a richer caller may set later,
    but ``IN_PROGRESS`` is **not** recorded (the α8.3 completion service records the
    terminal outcome under the same ``request_id``).
    """

    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL = "partial"
    TIMEOUT = "timeout"


@dataclass(frozen=True, slots=True)
class RecordUsageCommand:
    """One terminal provider call to account for — the **application contract**.

    Carries the billing context the α7.4 ``ProviderResponse`` alone lacks
    (``tenant_id`` / ``model_id`` / workflow + render linkage). Per the α7.5
    sign-off (Q3) it deliberately includes fields with **no** ``usage_records``
    column (``render_job_id``): the command must serve future slices without a
    breaking change. ``status`` must be terminal (``SUCCEEDED`` / ``FAILED``); an
    ``IN_PROGRESS`` command is rejected (Q6). ``occurred_at`` defaults to now(UTC).
    """

    tenant_id: UUID
    model_id: UUID
    status: ProviderStatus
    request_id: str | None = None
    capability: Capability | None = None
    usage: ProviderUsage | None = None
    occurred_at: datetime | None = None
    project_id: UUID | None = None
    workflow_run_id: UUID | None = None
    workflow_step_id: UUID | None = None
    render_job_id: UUID | None = None
    user_id: UUID | None = None
    scene_id: UUID | None = None
    prompt_id: UUID | None = None
    latency_ms: int | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class UsageRecordView:
    """The priced summary :meth:`UsageRecorderPort.record` returns.

    ``idempotent_replay`` is ``True`` when a row for this ``request_id`` already
    existed and was returned unchanged (ADR-0033 dedupe) — the caller can treat the
    call as a no-op replay.
    """

    id: UUID
    occurred_at: datetime
    unit: str
    unit_count: Decimal
    estimated_cost: Decimal
    currency: str
    status: str
    idempotent_replay: bool


@dataclass(frozen=True, slots=True)
class NewUsageRecord:
    """The resolved, priced row to insert — neutral (the repo maps it to the ORM).

    ``render_job_id`` is intentionally **absent** (no column); the service stashes
    it into ``extra`` for traceability. ``credits_consumed`` is ``0`` in α7.5 (the
    ``credit_ledger`` debit is a later slice, Q1).
    """

    tenant_id: UUID
    model_id: UUID
    status: str
    unit: str
    unit_count: Decimal
    estimated_cost: Decimal
    currency: str
    occurred_at: datetime
    credits_consumed: Decimal = Decimal(0)
    request_id: str | None = None
    pricing_id: UUID | None = None
    tokens_prompt: int | None = None
    tokens_completion: int | None = None
    images_count: int | None = None
    seconds_generated: Decimal | None = None
    project_id: UUID | None = None
    scene_id: UUID | None = None
    prompt_id: UUID | None = None
    user_id: UUID | None = None
    workflow_run_id: UUID | None = None
    workflow_step_id: UUID | None = None
    latency_ms: int | None = None
    error_code: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class UsageRecordRow:
    """Immutable read-model of one persisted ``usage_records`` row."""

    id: UUID
    occurred_at: datetime
    tenant_id: UUID
    model_id: UUID
    request_id: str | None
    unit: str
    unit_count: Decimal
    estimated_cost: Decimal
    currency: str
    status: str
    pricing_id: UUID | None
    credits_consumed: Decimal
    tokens_prompt: int | None
    tokens_completion: int | None
    images_count: int | None
    seconds_generated: Decimal | None


@dataclass(frozen=True, slots=True)
class EffectivePrice:
    """One resolved ``ai_model_pricing`` row (the price effective at call time)."""

    pricing_id: UUID
    unit: str
    price_per_unit: Decimal
    currency: str


class DuplicateRequestIdError(Exception):
    """Raised by the usage repo when an insert collides with an existing ``request_id``.

    Signals the ADR-0033 per-partition ``uq_<child>_request_id`` was hit — a
    replay. The recorder catches it and returns the pre-existing row (idempotent).
    """

    def __init__(self, request_id: str | None) -> None:
        super().__init__(f"usage_records row already exists for request_id={request_id!r}")
        self.request_id = request_id


class UsageRecorderPort(ABC):
    """Record one terminal provider call as a priced ``usage_records`` row.

    Idempotent on ``request_id`` (a replay returns the existing row with
    ``idempotent_replay=True``). Purely observational (W7.5.1): the only write is
    ``usage_records``.
    """

    @abstractmethod
    async def record(self, command: RecordUsageCommand) -> UsageRecordView:
        """Price + persist ``command`` as one usage row; return its priced summary."""
        ...
