"""α8.6 Increment 5 — demo CLI for the end-to-end generation slice.

A thin convenience wrapper that invokes the **same** ``GenerateVideo`` use case the
integration test drives, wired through the real DI container — the image generator
is whatever the container configures (Pollinations today), the resolver reads the
seeded catalogue, and the Execution-Runtime store persists the run. This script
contains **no orchestration logic**: it builds a request, calls the use case, and
prints where to inspect the result. All the Prompt→Planner→Resolver→Generate→
Verify→Repair→Timeline→FFmpeg→Export logic lives behind the use case.

Point it at a throwaway / ephemeral PostgreSQL (``DATABASE_URL`` or ``--database-url``)
so you can inspect the persisted rows afterwards — nothing is cleaned up:

    generations, generation_shots, generation_assets,
    generation_resolution_ledger, event_outbox

Prerequisites: the target DB must be migrated to head (``alembic upgrade head``) and
carry a seeded ``image_generation`` adapter (``python scripts/seed_providers.py``).
ffmpeg + ffprobe must be installed.

Usage
-----
    python scripts/generate_demo.py
    python scripts/generate_demo.py --prompt "A neon city at night" --duration 6
    python scripts/generate_demo.py --mode free_remote_only --database-url postgresql+psycopg://…

Note: the current minimal storyboard reuses the same prompt+seed for every shot, so
a *deterministic* remote provider can produce identical frames and trip the timeline
duplicate gate on multi-shot runs. Use ``--duration`` equal to ``--per-shot`` (the
default) for a single-shot demo, or a larger duration to exercise the full scenario.
"""

from __future__ import annotations

import argparse
import asyncio
import os

from app.application.use_cases.generation.request import GenerateVideoRequest
from app.core import container
from app.core.config import Settings
from app.domain.generation.execution import ExecutionMode
from app.domain.generation.identity import GlobalStyle, IdentityProfile

_DEFAULT_PROMPT = "A little red fox walking through a snowy forest at sunrise."
_DEFAULT_SEED = 70707


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--prompt", default=_DEFAULT_PROMPT, help="the text prompt to generate")
    parser.add_argument("--seed", type=int, default=_DEFAULT_SEED, help="identity/base seed")
    parser.add_argument(
        "--mode",
        default=ExecutionMode.FREE_REMOTE_ONLY.value,
        choices=[m.value for m in ExecutionMode],
        help="execution mode (which adapter tiers may run)",
    )
    parser.add_argument(
        "--duration", type=float, default=3.0, help="target total video duration (seconds)"
    )
    parser.add_argument("--per-shot", type=float, default=3.0, help="per-shot duration (seconds)")
    parser.add_argument("--aspect-ratio", default="9:16", help="target aspect ratio")
    parser.add_argument("--platform", default="reel", help="target platform")
    parser.add_argument("--database-url", default=None, help="override DATABASE_URL for this run")
    return parser.parse_args()


def _build_request(args: argparse.Namespace) -> GenerateVideoRequest:
    identity = IdentityProfile(seed=args.seed, global_style=GlobalStyle.PIXAR)
    return GenerateVideoRequest(
        prompt=args.prompt,
        identity=identity,
        execution_mode=ExecutionMode(args.mode),
        aspect_ratio=args.aspect_ratio,
        target_platform=args.platform,
        target_duration_seconds=args.duration,
        per_shot_seconds=args.per_shot,
    )


async def _run(args: argparse.Namespace) -> int:
    settings = Settings()  # type: ignore[call-arg]
    container.reset()
    container.init(settings)
    try:
        request = _build_request(args)
        factory = container.get_session_factory()
        async with factory() as session:
            use_case = container.get_generate_video_use_case(session)
            result = await use_case.execute(request)
    finally:
        await container.shutdown()

    print()
    print("=" * 72)
    print(f"generation_id : {result.generation_id}")
    print(f"status        : {result.status.value}")
    print(f"title         : {result.title}")
    prov = result.provenance
    print(f"execution_mode: {prov.execution_mode}")
    print(
        f"chosen adapter: {prov.chosen_adapter} (provider={prov.chosen_provider}, "
        f"tier={prov.execution_tier})"
    )
    print(f"candidates    : {', '.join(prov.candidate_adapters) or '(none)'}")
    print(
        f"shots         : {sum(1 for s in result.shots if s.accepted)}/{len(result.shots)} "
        f"accepted"
    )
    if result.status.value == "succeeded":
        print(f"video_key     : {result.video_key}")
        print(f"dimensions    : {result.width}x{result.height} @ {result.duration_seconds}s")
    else:
        print(f"reason        : {result.reason}")
        if result.checks:
            print(f"checks        : {'; '.join(result.checks)}")
    print("=" * 72)
    print("Inspect the persisted run (no cleanup) with, e.g.:")
    gid = result.generation_id
    print(f"  SELECT status, chosen_adapter FROM generations WHERE id = '{gid}';")
    print(
        f"  SELECT shot_number, accepted, seed FROM generation_shots "
        f"WHERE generation_id = '{gid}' ORDER BY shot_number;"
    )
    print(
        f"  SELECT asset_kind, storage_key FROM generation_assets "
        f"WHERE generation_id = '{gid}';"
    )
    print(
        f"  SELECT chosen_adapter, candidate_list FROM generation_resolution_ledger "
        f"WHERE generation_id = '{gid}';"
    )
    print(f"  SELECT event_type FROM event_outbox WHERE aggregate_id = '{gid}';")
    return 0 if result.status.value == "succeeded" else 1


def main() -> int:
    args = _parse_args()
    if args.database_url:
        os.environ["DATABASE_URL"] = args.database_url
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
