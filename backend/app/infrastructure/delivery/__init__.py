"""Download-delivery adapters (Slice α8.5b.1).

α8.5b.1 ships the local streaming adapter only; cloud (signed-URL redirect) adapters arrive
in α8.5b.2 behind the same :class:`~app.application.interfaces.download_delivery.IDownloadDelivery`
port.
"""

from __future__ import annotations

from app.infrastructure.delivery.local_stream_delivery import LocalStreamDelivery

__all__ = ["LocalStreamDelivery"]
