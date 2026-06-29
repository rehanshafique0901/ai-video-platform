"""Load DATABASE_URL from .env.validation into the current process env.

Imported by every Step B validation script so that the credential never has to
appear on a shell command line. Safe to import multiple times.
"""

from __future__ import annotations

import os
from pathlib import Path


def load() -> str:
    here = Path(__file__).resolve().parent.parent  # backend/
    env_file = here / ".env.validation"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ[k.strip()] = v.strip()
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit(
            "DATABASE_URL is not set. Place it in backend/.env.validation or export it."
        )
    return url


if __name__ == "__main__":
    url = load()
    # Print a redacted form for sanity (never the raw password).
    import re

    redacted = re.sub(r"//([^:]+):[^@]+@", r"//\1:***@", url)
    print(redacted)
