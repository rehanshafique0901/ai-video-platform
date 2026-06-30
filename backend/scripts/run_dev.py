"""Local development server entry point.

Workaround for Windows: ``psycopg``'s async driver requires
``SelectorEventLoop``, but in Python 3.12+ uvicorn invokes
``asyncio.run(serve, loop_factory=...)`` which *bypasses* the global
event-loop policy — so setting ``WindowsSelectorEventLoopPolicy`` at
module-import time inside ``app.main`` has no effect when uvicorn is
launched via its CLI. This script constructs the uvicorn ``Server``
directly and passes ``asyncio.SelectorEventLoop`` as the loop factory
on Windows, then falls through to the default loop factory on
Linux / macOS (which is what production uses).

Usage (works on every platform)::

    python scripts/run_dev.py
    python scripts/run_dev.py --port 8001 --reload

CI runs ``uvicorn app.main:create_app --factory ...`` against the
Linux container in GitHub Actions; this script is purely for local
developer ergonomics.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable

import uvicorn


def _loop_factory() -> Callable[[], asyncio.AbstractEventLoop] | None:
    """Return an explicit Selector loop factory on Windows, else default."""
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop
    return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the FastAPI app locally (Windows-friendly).",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    config = uvicorn.Config(
        "app.main:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
    )
    server = uvicorn.Server(config)

    asyncio.run(server.serve(), loop_factory=_loop_factory())


if __name__ == "__main__":
    main()
