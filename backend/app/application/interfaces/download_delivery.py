"""Port: ``IDownloadDelivery`` — how an export artifact's bytes reach the client (α8.5b.1).

Download serving is a **pure read + transfer** of an already-stored delivery ``MediaAsset``
(produced by the α8.5a export engine). This port is the seam that keeps the download *endpoint
contract* stable while the *delivery mechanism* evolves:

* **α8.5b.1 (now)** — a single ``LocalStreamDelivery`` streams bytes through the API from
  ``IObjectStorage`` (fine for local / single-node deployments).
* **α8.5b.2 (later)** — cloud adapters (``S3Delivery`` / ``R2Delivery`` / …) return a
  :class:`RedirectDelivery` to a signed URL so the object store (or a CDN) serves the bytes,
  keeping large transfers off the API workers.

The port therefore returns a **decision** (stream *or* redirect), never raw bytes, so the
router renders whichever the adapter chose with **no endpoint change** when cloud delivery
arrives. Per the α8.5b.1 sign-off (Fork A) this slice introduces the seam but implements the
**local streaming** adapter only — **no** ``signed_url()`` / cloud / CDN code lives here.

W8.5b.1 (observational) / W8.5b.2 (pure transfer): delivery never encodes, transcodes,
re-composes, resizes, or mutates the artifact — it only moves already-final bytes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime


class DownloadDeliveryError(Exception):
    """The artifact could not be prepared for delivery (missing object, backend mismatch)."""


@dataclass(frozen=True, slots=True)
class DownloadRequest:
    """The stored object to deliver + its presentation metadata.

    Built by the use case from the delivery ``MediaAsset`` (storage triple + ``mime_type`` +
    ``size_bytes``). Carries no domain identity — the delivery adapter only needs to know
    *where* the bytes are and *how* to present them.
    """

    storage_backend: str
    storage_bucket: str
    storage_key: str
    media_type: str
    filename: str
    content_length: int | None


@dataclass(frozen=True, slots=True)
class StreamDelivery:
    """Deliver by streaming bytes through the API (local / single-node — α8.5b.1)."""

    chunks: AsyncIterator[bytes]
    media_type: str
    filename: str
    content_length: int | None


@dataclass(frozen=True, slots=True)
class RedirectDelivery:
    """Deliver by redirecting the client to a (signed) URL.

    Reserved for the cloud adapters in α8.5b.2 — **not produced** by any α8.5b.1 adapter. The
    type exists now so the endpoint + router already handle it (the seam Fork A protects).
    """

    url: str
    expires_at: datetime | None


# The delivery adapter chooses one; the router renders whichever it returns.
DeliveryDecision = StreamDelivery | RedirectDelivery


class IDownloadDelivery(ABC):
    """Decide + prepare how one export artifact's bytes are delivered to the client."""

    @abstractmethod
    async def deliver(self, request: DownloadRequest) -> DeliveryDecision:
        """Prepare delivery of the object at ``request`` and return a :data:`DeliveryDecision`.

        Raises :class:`DownloadDeliveryError` when the object cannot be prepared (e.g. it is
        not in a location this adapter serves). Adapters must **not** encode/transform the
        bytes (W8.5b.2) — they only move or reference the already-final artifact.
        """
        ...
