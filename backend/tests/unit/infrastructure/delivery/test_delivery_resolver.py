"""Unit tests for ``DeliveryResolver`` (Slice α8.5b.2).

The backend → delivery registry that is itself an ``IDownloadDelivery`` facade, so
``DownloadExport`` stays unchanged. Pins (W8.5b.4):

* dispatch is a pure function of ``request.storage_backend`` (local → stream adapter,
  s3/r2 → redirect adapter);
* an unconfigured backend raises ``DownloadDeliveryError`` (surfaced as 404 upstream).
"""

from __future__ import annotations

import pytest

from app.application.interfaces.download_delivery import (
    DeliveryDecision,
    DownloadDeliveryError,
    DownloadRequest,
    IDownloadDelivery,
    RedirectDelivery,
    StreamDelivery,
)
from app.infrastructure.delivery import DeliveryResolver

pytestmark = pytest.mark.unit


class _RecordingDelivery(IDownloadDelivery):
    def __init__(self, decision: DeliveryDecision) -> None:
        self._decision = decision
        self.seen: list[DownloadRequest] = []

    async def deliver(self, request: DownloadRequest) -> DeliveryDecision:
        self.seen.append(request)
        return self._decision


def _request(backend: str) -> DownloadRequest:
    return DownloadRequest(
        storage_backend=backend,
        storage_bucket="media",
        storage_key="k",
        media_type="video/mp4",
        filename="out.mp4",
        content_length=3,
    )


async def _empty() -> AsyncIterator[bytes]:  # type: ignore[name-defined]  # noqa: F821
    if False:
        yield b""


async def test_dispatches_to_backend_specific_adapter() -> None:
    stream = _RecordingDelivery(
        StreamDelivery(chunks=_empty(), media_type="video/mp4", filename="o", content_length=0)
    )
    redirect = _RecordingDelivery(RedirectDelivery(url="https://s/x", expires_at=None))
    resolver = DeliveryResolver(adapters={"local": stream, "s3": redirect})

    decision = await resolver.deliver(_request("s3"))

    assert isinstance(decision, RedirectDelivery)
    assert len(redirect.seen) == 1 and len(stream.seen) == 0


async def test_unknown_backend_raises_delivery_error() -> None:
    stream = _RecordingDelivery(
        StreamDelivery(chunks=_empty(), media_type="video/mp4", filename="o", content_length=0)
    )
    resolver = DeliveryResolver(adapters={"local": stream})

    with pytest.raises(DownloadDeliveryError):
        await resolver.deliver(_request("r2"))
