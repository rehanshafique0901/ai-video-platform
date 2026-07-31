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

**α10.0 — binding a world.** A request may name one of the caller's identity profiles. The
profile is read once, here, and **serialised whole into the request payload** (ADR-0055 D2,
IDENT-1): the generation carries a *value*, not a reference, so editing or deleting that world
tomorrow cannot change what this generation executed or what it would replay as. Nothing
downstream reads the profile again — the snapshot is the only channel (frozen decisions 4–5).

Identity is deliberately **not** part of the idempotency key (frozen decision 14): a replayed
key returns the original generation with its original world, even if the second call names a
different one. Content is never hashed.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, replace
from uuid import UUID, uuid4

from app.application.interfaces.generation_job_store import (
    GenerationView,
    IGenerationJobStore,
)
from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.generation.request_codec import (
    CharacterSnapshot,
    EntitySnapshot,
    GenerationRequestSpec,
    IdentitySnapshot,
)
from app.core.errors import NotFoundError
from app.domain.identity_runtime import IdentityProfile

# Comfortably inside `generations.seed` (bigint) and every provider's accepted range.
_SEED_BOUND = 2**31


@dataclass(frozen=True, slots=True)
class CreateGenerationResult:
    """The queued generation, plus whether this call created it (201) or replayed one (200)."""

    generation: GenerationView
    created: bool


class CreateGeneration:
    """Queue an owner-scoped generation (idempotent on the caller's ``idempotency_key``)."""

    def __init__(self, store: IGenerationJobStore, *, uow: IUnitOfWork | None = None) -> None:
        self._store = store
        # The unit of work is needed only to read a named world (α10.0). Direct-invocation
        # callers — the demo script, the pre-α10.0 tests — never name one and construct this
        # use case with the store alone; a request that names a world without a unit of work
        # is a composition error and says so.
        self._uow = uow

    async def execute(
        self,
        *,
        tenant_id: UUID,
        owner_user_id: UUID,
        spec: GenerationRequestSpec,
        identity_id: UUID | None = None,
        requested_seed: int | None = None,
        idempotency_key: str | None = None,
    ) -> CreateGenerationResult:
        if identity_id is not None:
            spec = await self._bind_world(
                spec,
                identity_id=identity_id,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                requested_seed=requested_seed,
            )
        outcome = await self._store.create(
            generation_id=uuid4(),
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            spec=spec,
            idempotency_key=idempotency_key,
        )
        return CreateGenerationResult(generation=outcome.view, created=outcome.created)

    async def _bind_world(
        self,
        spec: GenerationRequestSpec,
        *,
        identity_id: UUID,
        tenant_id: UUID,
        owner_user_id: UUID,
        requested_seed: int | None,
    ) -> GenerationRequestSpec:
        """Read the named world once and fold it into the request.

        The seed follows the precedence the pre-flight fixes: an explicit request seed, then
        the world's own, then the one already drawn. Whichever wins is written to the flat
        ``seed`` field — the single value ``generations.seed`` and the runtime both read, so
        the two can never disagree (ADR-0055 D4).
        """
        if self._uow is None:  # pragma: no cover - a composition error, not a request error
            raise RuntimeError("CreateGeneration needs a unit of work to bind an identity")
        async with self._uow:
            profile = await self._uow.identities.get_profile(identity_id, tenant_id, owner_user_id)
        if profile is None:
            raise NotFoundError(
                "identity profile not found", details={"identity_id": str(identity_id)}
            )
        return replace(
            spec,
            seed=spec.seed if requested_seed is not None else profile.seed,
            identity=snapshot_of(profile),
        )


def snapshot_of(profile: IdentityProfile) -> IdentitySnapshot:
    """Freeze an authored world into the value a generation carries.

    Children keep the profile's canonical order and their stable keys, so the shot records a
    worker writes minutes later name the same characters the creator named. ``version`` records
    which state of the world this is; nothing reads it back to look the world up again.
    """
    return IdentitySnapshot(
        identity_id=str(profile.id),
        version=profile.version,
        name=profile.name,
        seed=profile.seed,
        global_style=profile.global_style.value,
        camera_style=profile.camera_style,
        lighting=profile.lighting,
        color_palette=profile.color_palette,
        negative_prompt=profile.negative_prompt,
        characters=tuple(
            CharacterSnapshot(
                key=c.character_key,
                name=c.name,
                age=c.age,
                appearance=c.appearance,
                clothing=c.clothing,
                accessories=c.accessories,
            )
            for c in profile.characters
        ),
        locations=tuple(
            EntitySnapshot(key=loc.location_key, name=loc.name, descriptors=loc.descriptors)
            for loc in profile.locations
        ),
        props=tuple(
            EntitySnapshot(key=p.prop_key, name=p.name, descriptors=p.descriptors)
            for p in profile.props
        ),
    )


def resolve_seed(seed: int | None) -> int:
    """Return the caller's seed, or draw one to persist with the request."""
    return seed if seed is not None else secrets.randbelow(_SEED_BOUND)
