"""Unit tests for the Fal webhook verifier (Slice α8.3b).

Every test drives :class:`FalWebhookVerifier` through an in-memory
``httpx.MockTransport`` (the JWKS endpoint) and a frozen clock — **no network**.
We generate a real ED25519 keypair, publish its public key as the JWKS, and sign
the canonical Fal message so the happy path exercises genuine cryptographic
verification. They pin:

* a correctly-signed, fresh webhook → ``VerifiedWebhook(provider_job_id=request_id)``,
* tampered body / wrong key → 401 (``WebhookVerificationError``),
* missing header / non-hex signature → 401,
* stale timestamp (outside tolerance) → 401,
* JWKS key rotation (a second key verifies) → pass,
* JWKS is cached (one fetch across two verifies).
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.application.interfaces.clock import IClock
from app.application.interfaces.webhook_verifier import WebhookVerificationError
from app.infrastructure.ai.providers.fal.webhook import FalWebhookVerifier

pytestmark = pytest.mark.unit

_JWKS_URL = "https://rest.fal.ai/.well-known/jwks.json"
_NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=UTC)


class _FixedClock(IClock):
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


def _b64url_no_pad(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _public_bytes(key: Ed25519PrivateKey) -> bytes:
    from cryptography.hazmat.primitives import serialization

    return key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _jwks(*keys: Ed25519PrivateKey) -> dict[str, object]:
    return {
        "keys": [
            {"kty": "OKP", "crv": "Ed25519", "x": _b64url_no_pad(_public_bytes(k))} for k in keys
        ]
    }


def _sign(
    key: Ed25519PrivateKey,
    *,
    request_id: str,
    user_id: str,
    timestamp: str,
    body: bytes,
) -> str:
    body_hash = hashlib.sha256(body).hexdigest()
    message = "\n".join([request_id, user_id, timestamp, body_hash]).encode("utf-8")
    return key.sign(message).hex()


class _JwksServer:
    """Records how many times the JWKS was fetched (to prove caching)."""

    def __init__(self, document: dict[str, object]) -> None:
        self.document = document
        self.fetches = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.fetches += 1
        return httpx.Response(200, json=self.document)


def _verifier(
    server: _JwksServer, *, clock: IClock | None = None, tolerance: int = 300
) -> FalWebhookVerifier:
    client = httpx.AsyncClient(transport=httpx.MockTransport(server))
    return FalWebhookVerifier(
        client=client,
        jwks_url=_JWKS_URL,
        clock=clock or _FixedClock(_NOW),
        timestamp_tolerance_seconds=tolerance,
    )


def _headers(request_id: str, user_id: str, timestamp: str, signature_hex: str) -> dict[str, str]:
    return {
        "x-fal-webhook-request-id": request_id,
        "x-fal-webhook-user-id": user_id,
        "x-fal-webhook-timestamp": timestamp,
        "x-fal-webhook-signature": signature_hex,
    }


async def test_valid_signature_returns_provider_job_id() -> None:
    key = Ed25519PrivateKey.generate()
    server = _JwksServer(_jwks(key))
    verifier = _verifier(server)
    body = json.dumps({"status": "OK", "request_id": "fal-req-1"}).encode()
    ts = str(int(_NOW.timestamp()))
    sig = _sign(key, request_id="fal-req-1", user_id="user-9", timestamp=ts, body=body)

    verified = await verifier.verify(body=body, headers=_headers("fal-req-1", "user-9", ts, sig))

    assert verified.provider_job_id == "fal-req-1"


async def test_tampered_body_fails() -> None:
    key = Ed25519PrivateKey.generate()
    verifier = _verifier(_JwksServer(_jwks(key)))
    ts = str(int(_NOW.timestamp()))
    sig = _sign(key, request_id="r", user_id="u", timestamp=ts, body=b"original")

    with pytest.raises(WebhookVerificationError):
        await verifier.verify(body=b"TAMPERED", headers=_headers("r", "u", ts, sig))


async def test_wrong_key_fails() -> None:
    signing_key = Ed25519PrivateKey.generate()
    other_key = Ed25519PrivateKey.generate()
    verifier = _verifier(_JwksServer(_jwks(other_key)))  # JWKS advertises a DIFFERENT key
    body = b"{}"
    ts = str(int(_NOW.timestamp()))
    sig = _sign(signing_key, request_id="r", user_id="u", timestamp=ts, body=body)

    with pytest.raises(WebhookVerificationError):
        await verifier.verify(body=body, headers=_headers("r", "u", ts, sig))


async def test_missing_header_fails() -> None:
    key = Ed25519PrivateKey.generate()
    verifier = _verifier(_JwksServer(_jwks(key)))
    headers = _headers("r", "u", str(int(_NOW.timestamp())), "aa")
    del headers["x-fal-webhook-signature"]

    with pytest.raises(WebhookVerificationError):
        await verifier.verify(body=b"{}", headers=headers)


async def test_non_hex_signature_fails() -> None:
    key = Ed25519PrivateKey.generate()
    verifier = _verifier(_JwksServer(_jwks(key)))
    ts = str(int(_NOW.timestamp()))

    with pytest.raises(WebhookVerificationError):
        await verifier.verify(body=b"{}", headers=_headers("r", "u", ts, "not-hex!!"))


async def test_stale_timestamp_fails() -> None:
    key = Ed25519PrivateKey.generate()
    verifier = _verifier(_JwksServer(_jwks(key)), tolerance=300)
    stale_ts = str(int(_NOW.timestamp()) - 3600)  # 1h old, tolerance 5m
    body = b"{}"
    sig = _sign(key, request_id="r", user_id="u", timestamp=stale_ts, body=body)

    with pytest.raises(WebhookVerificationError):
        await verifier.verify(body=body, headers=_headers("r", "u", stale_ts, sig))


async def test_key_rotation_second_key_verifies() -> None:
    old_key = Ed25519PrivateKey.generate()
    new_key = Ed25519PrivateKey.generate()
    # JWKS advertises BOTH keys; the webhook is signed with the second.
    verifier = _verifier(_JwksServer(_jwks(old_key, new_key)))
    body = b"{}"
    ts = str(int(_NOW.timestamp()))
    sig = _sign(new_key, request_id="rot", user_id="u", timestamp=ts, body=body)

    verified = await verifier.verify(body=body, headers=_headers("rot", "u", ts, sig))
    assert verified.provider_job_id == "rot"


async def test_jwks_is_cached_across_verifies() -> None:
    key = Ed25519PrivateKey.generate()
    server = _JwksServer(_jwks(key))
    verifier = _verifier(server)
    body = b"{}"
    ts = str(int(_NOW.timestamp()))
    sig = _sign(key, request_id="r", user_id="u", timestamp=ts, body=body)

    await verifier.verify(body=body, headers=_headers("r", "u", ts, sig))
    await verifier.verify(body=body, headers=_headers("r", "u", ts, sig))

    assert server.fetches == 1  # second verify used the cache
