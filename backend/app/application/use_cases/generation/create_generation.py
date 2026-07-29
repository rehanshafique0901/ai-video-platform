"""α9.7 — generation ingress: queue a creator-requested video generation (ADR-0052).

The write half of the slice. It does one thing: durably record *what the creator asked for and
who asked for it*, as a `queued` row. No provider is contacted, no money is spent, and the call
returns in milliseconds — a generation runs for minutes (ADR-0052 D2/F4), so execution belongs
to the worker, not to the request.

Two properties are worth naming:

* **The seed is resolved here, once.** If the caller supplies none, one is drawn and persisted
  with the request, so a claimed row always replays to the identical runtime request and an
  idempotent replay returns a genuinely identical generation rather than a similar one.
* **Idempotency is the creator's explicit intent, never inferred from content** (ADR-0052 D4).
  Asking twice for the same prompt is *the* iteration loop of a generative product; a second
  request with no key — or with a new key — is a second generation, deliberately.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from uuid import UUID, uuid4

from app.application.interfaces.generation_job_store import (
    GenerationView,
    IGenerationJobStore,
)
from app.application.use_cases.generation.request_codec import GenerationRequestSpec

# Comfortably inside `generations.seed` (bigint) and every provider's accepted range.
_SEED_BOUND = 2**31


@dataclass(frozen=True, slots=True)
class CreateGenerationResult:
    """The queued generation, plus whether this call created it (201) or replayed one (200)."""

    generation: GenerationView
    created: bool


class CreateGeneration:
    """Queue an owner-scoped generation (idempotent on the caller's ``idempotency_key``)."""

    def __init__(self, store: IGenerationJobStore) -> None:
        self._store = store

    async def execute(
        self,
        *,
        tenant_id: UUID,
        owner_user_id: UUID,
        spec: GenerationRequestSpec,
        idempotency_key: str | None = None,
    ) -> CreateGenerationResult:
        outcome = await self._store.create(
            generation_id=uuid4(),
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            spec=spec,
            idempotency_key=idempotency_key,
        )
        return CreateGenerationResult(generation=outcome.view, created=outcome.created)


def resolve_seed(seed: int | None) -> int:
    """Return the caller's seed, or draw one to persist with the request."""
    return seed if seed is not None else secrets.randbelow(_SEED_BOUND)
