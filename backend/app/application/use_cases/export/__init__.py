"""Export use cases — the delivery-encoding engine (Slice α8.5a).

Strictly downstream of render/enrichment: an export transcodes a completed render's master
``MediaAsset`` into a replaceable delivery artifact (W8.5.1 / W8.5.3). Follows the platform's
claim → lease → transform → idempotent-settle → event worker model, entirely outside the
ADR-0042 frozen orchestration surface and within ADR-0043 RC5/RC6.
"""

from __future__ import annotations
