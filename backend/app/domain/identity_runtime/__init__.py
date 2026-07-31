"""Identity Runtime — the creator-authored world a generation can be bound to.

A *profile* is one creator's durable world: named characters with a stable
appearance, the location they inhabit, recurring props, the project-wide look,
and a stable seed. ADR-0055 D1 places this context in the **Knowledge** plane —
it declares what exists, the way the provider catalogue declares what can be
executed. The Decision plane consumes it; Execution never sees it.

The package is ``identity_runtime`` rather than ``identity`` because
``app/domain/identity/`` is the authentication context (``User``, ``Tenant``,
``Session``). They are different bounded contexts and share nothing.

Not to be confused with :mod:`app.domain.generation.identity`, which holds the
*immutable value object* the planner and prompt builder consume. That one is a
generation's world; this one is the creator's, and ingress snapshots this into
that (ADR-0055 D2, IDENT-1).

What a profile never holds (ADR-0055 D1, IDENT-4): execution history, planner
decisions, adapter or provider preference, adapter health, success statistics,
verification outcomes.
"""

from app.domain.identity_runtime.profile import (
    MAX_CHARACTERS,
    MAX_LOCATIONS,
    MAX_PROPS,
    SEED_BOUND,
    Character,
    IdentityProfile,
    IdentityValidationError,
    Location,
    Prop,
    ensure_unique_keys,
    ensure_within_caps,
    in_canonical_order,
)

__all__ = [
    "MAX_CHARACTERS",
    "MAX_LOCATIONS",
    "MAX_PROPS",
    "SEED_BOUND",
    "Character",
    "IdentityProfile",
    "IdentityValidationError",
    "Location",
    "Prop",
    "ensure_unique_keys",
    "ensure_within_caps",
    "in_canonical_order",
]
