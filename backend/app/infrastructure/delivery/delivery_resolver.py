"""``DeliveryResolver`` — backend → delivery adapter registry (Slice α8.5b.2).

Itself an :class:`IDownloadDelivery`: a thin resolving facade that dispatches
:meth:`deliver` on ``request.storage_backend`` (carried on the :class:`DownloadRequest`, which
comes straight from the artifact's persisted ``MediaAsset.storage_backend``). Because it *is* an
``IDownloadDelivery``, :class:`DownloadExport` is injected with it unchanged and stays entirely
backend-unaware (α8.5b.2 Ruling A + Ruling E — endpoint stability).

Delivery selection is therefore a pure function of the persisted backend (W8.5b.4): local →
:class:`LocalStreamDelivery` (stream), s3/r2 → :class:`S3RedirectDelivery` (302 redirect). No
cloud SDK import here — the adapters own that (Ruling D).
"""

from __future__ import annotations

from app.application.interfaces.download_delivery import (
    DeliveryDecision,
    DownloadDeliveryError,
    DownloadRequest,
    IDownloadDelivery,
)


class DeliveryResolver(IDownloadDelivery):
    """Dispatch a download to the delivery adapter for the artifact's persisted backend."""

    def __init__(self, *, adapters: dict[str, IDownloadDelivery]) -> None:
        self._adapters = dict(adapters)

    async def deliver(self, request: DownloadRequest) -> DeliveryDecision:
        try:
            adapter = self._adapters[request.storage_backend]
        except KeyError as exc:
            raise DownloadDeliveryError(
                f"no delivery adapter configured for backend {request.storage_backend!r}"
            ) from exc
        return await adapter.deliver(request)
