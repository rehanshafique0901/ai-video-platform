"""Fal.ai inbound webhook verifier — ED25519 over a JWKS trust anchor (Slice α8.3b).

Fal signs every webhook with ED25519 and publishes the **public** verification
keys at a JWKS endpoint. This adapter authenticates a delivery and extracts the
Fal ``request_id`` (= our ``provider_job_id``) so the ingress use case can locate
the paused run and trigger the frozen completion pipeline (W8.3b.1).

Verification (per fal.ai docs):
  1. Require headers ``X-Fal-Webhook-{Request-Id,User-Id,Timestamp,Signature}``.
  2. Reject if ``|now - timestamp| > tolerance`` (replay guard).
  3. Build ``message = "\\n".join([request_id, user_id, timestamp, sha256_hex(body)])``.
  4. Verify the hex signature (detached ED25519) against **any** JWKS public key.

W8.1.1 clarification: the invariant governs *credentials / authentication
material*. The JWKS holds **public** verification keys — configuration-independent
trust anchors — so fetching + caching them here is permitted and injects no
secret. This is a strict leaf: it imports only ``httpx`` + ``cryptography`` + the
neutral application port; no orchestration / api / workflow-domain import.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from collections.abc import Mapping
from datetime import datetime

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.application.interfaces.clock import IClock
from app.application.interfaces.webhook_verifier import (
    IWebhookVerifier,
    VerifiedWebhook,
    WebhookMalformedError,
    WebhookVerificationError,
)

_REQUIRED_HEADERS = (
    "x-fal-webhook-request-id",
    "x-fal-webhook-user-id",
    "x-fal-webhook-timestamp",
    "x-fal-webhook-signature",
)


def _b64url_decode(value: str) -> bytes:
    """Decode a base64url string, tolerating missing padding."""
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _any_key_verifies(keys: list[bytes], signature: bytes, message: bytes) -> bool:
    for raw in keys:
        try:
            Ed25519PublicKey.from_public_bytes(raw).verify(signature, message)
            return True
        except (InvalidSignature, ValueError):
            continue
    return False


class FalWebhookVerifier(IWebhookVerifier):
    """Verify Fal webhooks with cached ED25519 JWKS public keys."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        jwks_url: str,
        clock: IClock,
        timestamp_tolerance_seconds: int = 300,
        jwks_cache_seconds: float = 3600.0,
    ) -> None:
        # ``client`` is injected (like the α8.2 Fal client) so tests drive it with
        # an in-memory MockTransport — no network. The JWKS URL is absolute.
        self._client = client
        self._jwks_url = jwks_url
        self._clock = clock
        self._tolerance = timestamp_tolerance_seconds
        self._cache_ttl = jwks_cache_seconds
        self._cached_keys: list[bytes] = []
        self._fetched_at: datetime | None = None

    async def verify(self, *, body: bytes, headers: Mapping[str, str]) -> VerifiedWebhook:
        h = {str(k).lower(): str(v) for k, v in headers.items()}
        missing = [name for name in _REQUIRED_HEADERS if not h.get(name)]
        if missing:
            raise WebhookVerificationError(f"missing webhook headers: {missing}")

        request_id = h["x-fal-webhook-request-id"]
        user_id = h["x-fal-webhook-user-id"]
        timestamp = h["x-fal-webhook-timestamp"]
        signature_hex = h["x-fal-webhook-signature"]

        # Replay guard — timestamp must be within tolerance of now.
        try:
            ts = int(timestamp)
        except ValueError as exc:
            raise WebhookVerificationError("non-integer webhook timestamp") from exc
        now = int(self._clock.now().timestamp())
        if abs(now - ts) > self._tolerance:
            raise WebhookVerificationError("webhook timestamp outside tolerance")

        body_hash = hashlib.sha256(body).hexdigest()
        message = "\n".join([request_id, user_id, timestamp, body_hash]).encode("utf-8")
        try:
            signature = bytes.fromhex(signature_hex)
        except ValueError as exc:
            raise WebhookVerificationError("signature is not valid hex") from exc

        keys = await self._public_keys()
        if not _any_key_verifies(keys, signature, message):
            raise WebhookVerificationError("no JWKS key verifies the signature")

        if not request_id:
            raise WebhookMalformedError("verified webhook carries no request id")
        return VerifiedWebhook(provider_job_id=request_id)

    async def _public_keys(self) -> list[bytes]:
        """Return cached ED25519 public keys, refreshing from the JWKS when stale."""
        if self._is_cache_fresh():
            return self._cached_keys
        try:
            response = await self._client.get(self._jwks_url)
        except httpx.HTTPError as exc:
            raise WebhookVerificationError(f"JWKS fetch failed: {exc}") from exc
        if response.status_code != httpx.codes.OK:
            raise WebhookVerificationError(f"JWKS endpoint returned {response.status_code}")
        try:
            document = response.json()
        except ValueError as exc:
            raise WebhookVerificationError("JWKS response is not JSON") from exc

        keys: list[bytes] = []
        for entry in document.get("keys", []) if isinstance(document, dict) else []:
            x = entry.get("x") if isinstance(entry, dict) else None
            if isinstance(x, str):
                try:
                    keys.append(_b64url_decode(x))
                except (ValueError, binascii.Error):
                    continue
        if not keys:
            raise WebhookVerificationError("JWKS contained no usable ED25519 keys")

        self._cached_keys = keys
        self._fetched_at = self._clock.now()
        return keys

    def _is_cache_fresh(self) -> bool:
        if self._fetched_at is None or not self._cached_keys:
            return False
        age = (self._clock.now() - self._fetched_at).total_seconds()
        return age < self._cache_ttl
