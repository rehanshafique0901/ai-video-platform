"""Worker runtime — the process that executes background work (α9.8, ADR-0053).

``app.runtime`` is a **delivery layer**, peer to ``app.api``. The API turns HTTP requests into
application calls; the runtime turns elapsed time into application calls. Both depend on
``app.application`` + ``app.core`` and are depended on by nothing — an import-linter contract
pins that, so no use case can reach the scheduler even by accident (ADR-0053 PF1).

Until this package existed, **no background work ran anywhere outside tests**: every worker was a
library primitive whose ``run_once()`` had zero production callers.
"""

from app.runtime.liveness import Liveness
from app.runtime.worker_host import HostResult, WorkerHost, WorkerReport, WorkerSpec

__all__ = [
    "HostResult",
    "Liveness",
    "WorkerHost",
    "WorkerReport",
    "WorkerSpec",
]
