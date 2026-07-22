"""Port: ``IWebhookVerifier`` — inbound provider-webhook authentication (Slice α8.3b).

A webhook is a **trigger, not a source of truth** (W8.3b.1 / pre-flight Fork C1):
the verifier authenticates the request and extracts the single **resume
coordinate** — the provider job id — and nothing more. The frozen
``CompletionEngine.complete()`` then performs the authoritative resolve. The raw
payload is therefore never used to mutate workflow state.

The port is provider-agnostic and lives in the application layer so the ingress
use case depends on it without importing infrastructure. Concrete verifiers (e.g.
the Fal ED25519 / JWKS verifier) live in the provider leaf.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass


class WebhookVerificationError(Exception):
    """The webhook could not be authenticated (missing/malformed/stale/invalid signature).

    The router maps this to **HTTP 401**. The payload is discarded — an
    unverifiable request never reaches the completion pipeline.
    """


class WebhookMalformedError(Exception):
    """The webhook is authentic but structurally unusable (no resume coordinate).

    The router maps this to **HTTP 400**.
    """


@dataclass(frozen=True, slots=True)
class VerifiedWebhook:
    """The trusted minimum extracted from a verified webhook.

    Only ``provider_job_id`` — the coordinate that locates the paused run. Per the
    trigger-only design the delivered result body is intentionally *not* returned.
    """

    provider_job_id: str


class IWebhookVerifier(ABC):
    """Authenticate an inbound provider webhook and extract its resume coordinate."""

    @abstractmethod
    async def verify(self, *, body: bytes, headers: Mapping[str, str]) -> VerifiedWebhook:
        """Verify signature + freshness over the **raw** body; return the coordinate.

        ``body`` MUST be the exact bytes received (the signature covers a hash of
        them). ``headers`` are the request headers (lower-cased keys tolerated).

        Raises:
            WebhookVerificationError: bad/missing/stale signature or headers (→ 401).
            WebhookMalformedError: verified but no usable resume coordinate (→ 400).
        """
        ...
