"""Orchestrate the full Phase 2 Step B schema validation cycle.

Implements the execution order from ``ROADMAP.md`` (Phase 2 — Step B):

  4. alembic upgrade head
  5. alembic downgrade base   (verifies clean revert)
  6. alembic upgrade head     (verifies idempotency)
  7. introspect schema, run integrity checks (validate_schema.py)
  8. regenerate ERD (regenerate_erd.py)
  9. produce schema_validation_report.json + erd_generated.md

Run with: ``python scripts/run_validation.py``

Requires a running Postgres reachable via ``DATABASE_URL`` (see
``docker-compose.db.yml``).
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import sqlalchemy as sa

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _load_env

ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = ROOT.parent / "validation_artifacts"
ARTIFACTS.mkdir(exist_ok=True)

DATABASE_URL = _load_env.load()


def run(cmd: str | list[str], *, cwd: Path = ROOT, check: bool = True) -> int:
    if isinstance(cmd, str):
        printable = cmd
        args = shlex.split(cmd) if os.name != "nt" else cmd
    else:
        printable = " ".join(shlex.quote(a) for a in cmd)
        args = cmd
    print(f"\n$ {printable}", flush=True)
    proc = subprocess.run(args, cwd=str(cwd), shell=isinstance(args, str))
    if check and proc.returncode != 0:
        raise SystemExit(f"command failed (rc={proc.returncode}): {printable}")
    return proc.returncode


def wait_for_db(retries: int = 60) -> None:
    eng = sa.create_engine(DATABASE_URL, future=True)
    for i in range(retries):
        try:
            with eng.connect() as c:
                c.execute(sa.text("SELECT 1"))
            print(f"database reachable after {i} retries")
            return
        except Exception as exc:
            print(f"  waiting for db ({i+1}/{retries}): {exc.__class__.__name__}")
            time.sleep(1)
    raise SystemExit("database did not become reachable")


def main() -> int:
    print("== Phase 2 Step B — schema validation runner ==")
    wait_for_db()

    # Step 4
    run("alembic upgrade head")

    # Step 5
    run("alembic downgrade base")

    # Step 6 (idempotency)
    run("alembic upgrade head")

    # Step 7
    rc = run(
        [
            sys.executable,
            "scripts/validate_schema.py",
            str(ARTIFACTS / "schema_validation_report.json"),
        ],
        check=False,
    )

    # Step 8
    run(
        [
            sys.executable,
            "scripts/regenerate_erd.py",
            str(ARTIFACTS / "erd_generated.md"),
        ]
    )

    print()
    if rc != 0:
        print("VALIDATION FAILED — see schema_validation_report.json", flush=True)
    else:
        print("VALIDATION PASSED", flush=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
