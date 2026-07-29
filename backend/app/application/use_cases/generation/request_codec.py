"""The α9.7 generation-request codec: creator intent ↔ durable JSON ↔ runtime request.

Generation ingress (`POST /api/v1/generations`) writes a **queued** row; a worker claims it
minutes later and must reconstruct the caller's :class:`GenerateVideoRequest` *exactly*. The
existing `generations` columns cannot express one — `target_duration_seconds`,
`per_shot_seconds`, `min_similarity` and `max_attempts` have no column, and `identity` is a
nested value object of which only `seed` is stored. So the request is persisted verbatim in
the ingress-owned `generations.request` JSONB column, the same way `publish_jobs` persists its
immutable `content_package` (pre-flight PF3).

**v1 scope.** :class:`GenerationRequestSpec` is deliberately *flat and scalar*. The identity
built from it carries only `seed` + `global_style` — no characters, locations, props, or
reference images. Full Identity-Runtime authoring needs its own persistence and API surface and
is a separate slice; folding it in here would triple this one. :func:`decode_spec` therefore
**rejects unknown keys** rather than ignoring them, so a later identity slice extends the
payload additively instead of silently reinterpreting rows written by this one.

The codec is pure: no I/O, no clock, no randomness. The seed is resolved *at ingress* and
persisted, so a claimed row always replays to the identical request.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from app.application.use_cases.generation.request import GenerateVideoRequest
from app.core.errors import ValidationFailedError
from app.domain.generation.execution import ExecutionMode
from app.domain.generation.identity import GlobalStyle, IdentityProfile

# Bumped only if the payload shape changes incompatibly; a row written by an older
# version is then explicitly migrated rather than silently misread.
SPEC_VERSION = 1


@dataclass(frozen=True, slots=True)
class GenerationRequestSpec:
    """The creator's asserted generation intent — the v1 wire + storage shape.

    Field defaults mirror :class:`GenerateVideoRequest` exactly, so a spec that omits
    everything but ``prompt`` reconstructs the same request the direct-invocation callers
    (demo script, tests) build by hand.
    """

    prompt: str
    seed: int
    title: str | None = None
    execution_mode: str = ExecutionMode.AUTO.value
    global_style: str = GlobalStyle.PIXAR.value
    aspect_ratio: str = "9:16"
    target_platform: str = "reel"
    target_duration_seconds: float = 18.0
    per_shot_seconds: float = 3.0
    width: int = 720
    height: int = 1280
    fps: int = 30


_SPEC_FIELDS = frozenset(GenerationRequestSpec.__dataclass_fields__)


def encode_spec(spec: GenerationRequestSpec) -> dict[str, Any]:
    """Serialise a spec into the JSONB payload stored in ``generations.request``."""
    payload = asdict(spec)
    payload["v"] = SPEC_VERSION
    return payload


def decode_spec(payload: dict[str, Any]) -> GenerationRequestSpec:
    """Rebuild a spec from a stored payload.

    Raises :class:`ValidationFailedError` on an unsupported version, an unknown key, or a
    missing required field. Failing loudly matters here: a misread request would silently
    generate something the creator never asked for, and would be billed as if they had.
    """
    version = payload.get("v")
    if version != SPEC_VERSION:
        raise ValidationFailedError(
            "unsupported generation request payload version",
            details={"version": version, "supported": SPEC_VERSION},
        )
    fields = {k: v for k, v in payload.items() if k != "v"}
    unknown = sorted(set(fields) - _SPEC_FIELDS)
    if unknown:
        raise ValidationFailedError(
            "unknown keys in generation request payload",
            details={"unknown": unknown},
        )
    try:
        return GenerationRequestSpec(**fields)
    except TypeError as exc:  # missing required field
        raise ValidationFailedError(
            "malformed generation request payload", details={"reason": str(exc)}
        ) from exc


def to_runtime_request(spec: GenerationRequestSpec, *, generation_id: Any) -> GenerateVideoRequest:
    """Build the runtime request the pipeline consumes.

    The identity is the v1 minimum — a stable ``seed`` plus the project-wide art style. Every
    other :class:`GenerateVideoRequest` field the spec does not carry keeps its dataclass
    default, which is precisely what the direct-invocation path already relies on.
    """
    return GenerateVideoRequest(
        prompt=spec.prompt,
        identity=IdentityProfile(seed=spec.seed, global_style=GlobalStyle(spec.global_style)),
        generation_id=generation_id,
        execution_mode=ExecutionMode(spec.execution_mode),
        aspect_ratio=spec.aspect_ratio,
        target_platform=spec.target_platform,
        target_duration_seconds=spec.target_duration_seconds,
        per_shot_seconds=spec.per_shot_seconds,
        title=spec.title,
        width=spec.width,
        height=spec.height,
        fps=spec.fps,
    )
