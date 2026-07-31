"""The α9.7 generation-request codec: creator intent ↔ durable JSON ↔ runtime request.

Generation ingress (`POST /api/v1/generations`) writes a **queued** row; a worker claims it
minutes later and must reconstruct the caller's :class:`GenerateVideoRequest` *exactly*. The
existing `generations` columns cannot express one — `target_duration_seconds`,
`per_shot_seconds`, `min_similarity` and `max_attempts` have no column, and `identity` is a
nested value object of which only `seed` is stored. So the request is persisted verbatim in
the ingress-owned `generations.request` JSONB column, the same way `publish_jobs` persists its
immutable `content_package` (pre-flight PF3).

**v2 (α10.0) adds one optional nested ``identity`` object** — the creator's authored world,
captured whole at the moment the request was accepted. It is a *value*, never a reference
(ADR-0055 D2, IDENT-1): editing or deleting the profile tomorrow cannot change what this
generation executed, or what it would replay as. ``identity_id`` and the profile's ``version``
travel inside it as provenance, with no foreign key (ADR-0046 X5).

**v1 rows are still read, and are never rewritten** (ADR-0055 D3, frozen decisions 9–11). A v1
payload decodes to a spec whose ``identity`` is absent and reconstructs exactly the ``seed`` +
``global_style`` profile it always did. Unknown keys remain a hard error at every level, so a
later slice extends the payload additively instead of silently reinterpreting rows written by
this one, and an undecodable payload fails loudly rather than generating a world the creator
never asked for (frozen decision 7).

**One authority per value.** Ingress resolves precedence once — an explicit request value, then
the profile's, then the default — and writes the result into the flat fields (``seed``,
``global_style``). The snapshot preserves what the *world* declared. So the runtime reads the
seed and the style from the flat fields and the cast, the place and the look from the snapshot,
and the two can never disagree about ``generations.seed`` (ADR-0055 D4).

The codec is pure: no I/O, no clock, no randomness. The seed is resolved *at ingress* and
persisted, so a claimed row always replays to the identical request.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, fields
from typing import Any, TypeVar

from app.application.use_cases.generation.request import GenerateVideoRequest
from app.core.errors import ValidationFailedError
from app.domain.generation.execution import ExecutionMode
from app.domain.generation.identity import (
    Character,
    GlobalStyle,
    IdentityProfile,
    Location,
    Prop,
)

# Bumped only if the payload shape changes incompatibly; a row written by an older
# version is then explicitly migrated rather than silently misread. v2 (α10.0) is
# additive — every v1 row still decodes, and none is rewritten.
SPEC_VERSION = 2

#: Versions this codec can read. A row written by any of them replays exactly as written.
SUPPORTED_SPEC_VERSIONS = (1, 2)


@dataclass(frozen=True, slots=True)
class CharacterSnapshot:
    """One character of the authored world, frozen at acceptance.

    ``key`` is the profile's stable child key — the same value the planner and shot
    records carry, so a later rename of ``name`` cannot orphan a reference.
    """

    key: str
    name: str
    age: str | None = None
    appearance: tuple[str, ...] = ()
    clothing: str | None = None
    accessories: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EntitySnapshot:
    """A location or a prop: the two carry the same shape (a key, a name, descriptors)."""

    key: str
    name: str
    descriptors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class IdentitySnapshot:
    """The creator's world as it was when this generation was accepted.

    ``identity_id`` and ``version`` say *which* world and *which state of it* — provenance
    values, not a live handle. ``seed`` is what the profile declared; the seed the run
    actually uses is the spec's, resolved once at ingress.
    """

    identity_id: str
    version: int
    name: str
    seed: int
    global_style: str
    camera_style: str | None = None
    lighting: str | None = None
    color_palette: str | None = None
    negative_prompt: str | None = None
    characters: tuple[CharacterSnapshot, ...] = ()
    locations: tuple[EntitySnapshot, ...] = ()
    props: tuple[EntitySnapshot, ...] = ()


@dataclass(frozen=True, slots=True)
class GenerationRequestSpec:
    """The creator's asserted generation intent — the wire + storage shape.

    Field defaults mirror :class:`GenerateVideoRequest` exactly, so a spec that omits
    everything but ``prompt`` reconstructs the same request the direct-invocation callers
    (demo script, tests) build by hand. ``identity`` is the only v2 addition; when it is
    absent the spec behaves exactly as a v1 one.
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
    identity: IdentitySnapshot | None = None


_SPEC_FIELDS = frozenset(GenerationRequestSpec.__dataclass_fields__)
#: Everything a v1 row was allowed to carry — the v2 addition excluded.
_V1_FIELDS = _SPEC_FIELDS - {"identity"}


def encode_spec(spec: GenerationRequestSpec) -> dict[str, Any]:
    """Serialise a spec into the JSONB payload stored in ``generations.request``.

    A spec with no identity encodes to exactly the v1 body plus ``"v": 2`` — the key is
    omitted rather than written as ``null``, so a payload never claims a world that was
    never named.
    """
    payload = asdict(spec)
    if spec.identity is None:
        del payload["identity"]
    payload["v"] = SPEC_VERSION
    return payload


def decode_spec(payload: dict[str, Any]) -> GenerationRequestSpec:
    """Rebuild a spec from a stored payload.

    Raises :class:`ValidationFailedError` on an unsupported version, an unknown key at any
    level, or a missing required field. Failing loudly matters here: a misread request would
    silently generate something the creator never asked for, and would be billed as if they
    had. A v1 payload is read as written — no identity is invented for it, and the row is
    not rewritten.
    """
    version = payload.get("v")
    if version not in SUPPORTED_SPEC_VERSIONS:
        raise ValidationFailedError(
            "unsupported generation request payload version",
            details={"version": version, "supported": list(SUPPORTED_SPEC_VERSIONS)},
        )
    fields_ = {k: v for k, v in payload.items() if k != "v"}
    allowed = _V1_FIELDS if version == 1 else _SPEC_FIELDS
    unknown = sorted(set(fields_) - allowed)
    if unknown:
        raise ValidationFailedError(
            "unknown keys in generation request payload",
            details={"unknown": unknown, "version": version},
        )
    identity = fields_.pop("identity", None)
    if identity is not None:
        fields_["identity"] = _decode_identity(identity)
    try:
        return GenerationRequestSpec(**fields_)
    except TypeError as exc:  # missing required field
        raise ValidationFailedError(
            "malformed generation request payload", details={"reason": str(exc)}
        ) from exc


def to_runtime_request(spec: GenerationRequestSpec, *, generation_id: Any) -> GenerateVideoRequest:
    """Build the runtime request the pipeline consumes.

    Without a snapshot the identity is the v1 minimum — a stable ``seed`` plus the
    project-wide art style — which is byte-for-byte what this function has always produced.
    With one, the same profile also carries the cast, the place, the props and the look the
    creator authored, rebuilt from the stored value and from nothing else: no lookup, no
    re-derivation, no second reader of the live profile.
    """
    return GenerateVideoRequest(
        prompt=spec.prompt,
        identity=_to_identity_profile(spec),
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


def _to_identity_profile(spec: GenerationRequestSpec) -> IdentityProfile:
    """The world this run executes against.

    ``seed`` and ``global_style`` come from the flat fields — the values ingress resolved
    and persisted — while everything else comes from the snapshot. The child ``key`` becomes
    the domain object's ``id``, which is what the planner and prompt builder address.
    """
    style = GlobalStyle(spec.global_style)
    snapshot = spec.identity
    if snapshot is None:
        return IdentityProfile(seed=spec.seed, global_style=style)
    return IdentityProfile(
        seed=spec.seed,
        global_style=style,
        characters=tuple(
            Character(
                id=c.key,
                name=c.name,
                age=c.age,
                appearance=c.appearance,
                clothing=c.clothing,
                accessories=c.accessories,
            )
            for c in snapshot.characters
        ),
        locations=tuple(
            Location(id=loc.key, name=loc.name, descriptors=loc.descriptors)
            for loc in snapshot.locations
        ),
        props=tuple(Prop(id=p.key, name=p.name, descriptors=p.descriptors) for p in snapshot.props),
        camera_style=snapshot.camera_style,
        lighting=snapshot.lighting,
        color_palette=snapshot.color_palette,
        negative_prompt=snapshot.negative_prompt,
    )


ChildT = TypeVar("ChildT", CharacterSnapshot, EntitySnapshot)

_CHILD_KEYS = ("characters", "locations", "props")

_TUPLE_FIELDS = {"appearance", "accessories", "descriptors"}


def _decode_identity(raw: Any) -> IdentitySnapshot:
    """Rebuild the snapshot, rejecting an unknown key at any depth."""
    if not isinstance(raw, Mapping):
        raise ValidationFailedError(
            "malformed identity snapshot in generation request payload",
            details={"reason": f"expected an object, got {type(raw).__name__}"},
        )
    known = {f.name for f in fields(IdentitySnapshot)}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ValidationFailedError(
            "unknown keys in identity snapshot", details={"unknown": unknown}
        )
    values: dict[str, Any] = {k: v for k, v in raw.items() if k not in _CHILD_KEYS}
    if "characters" in raw:
        values["characters"] = tuple(
            _decode_child(item, CharacterSnapshot, "characters")
            for item in _as_sequence(raw, "characters")
        )
    if "locations" in raw:
        values["locations"] = tuple(
            _decode_child(item, EntitySnapshot, "locations")
            for item in _as_sequence(raw, "locations")
        )
    if "props" in raw:
        values["props"] = tuple(
            _decode_child(item, EntitySnapshot, "props") for item in _as_sequence(raw, "props")
        )
    try:
        return IdentitySnapshot(**values)
    except TypeError as exc:
        raise ValidationFailedError(
            "malformed identity snapshot in generation request payload",
            details={"reason": str(exc)},
        ) from exc


def _as_sequence(raw: Mapping[str, Any], key: str) -> Sequence[Any]:
    value = raw[key]
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ValidationFailedError(
            "malformed identity snapshot in generation request payload",
            details={"reason": f"{key} must be a list"},
        )
    return list(value)


def _decode_child(raw: Any, child_cls: type[ChildT], kind: str) -> ChildT:
    if not isinstance(raw, Mapping):
        raise ValidationFailedError(
            "malformed identity snapshot in generation request payload",
            details={"reason": f"each {kind} entry must be an object"},
        )
    known = {f.name for f in fields(child_cls)}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ValidationFailedError(
            "unknown keys in identity snapshot", details={"unknown": unknown, "in": kind}
        )
    values: dict[str, Any] = {}
    for key, value in raw.items():
        if key in _TUPLE_FIELDS:
            if isinstance(value, str) or not isinstance(value, Sequence):
                raise ValidationFailedError(
                    "malformed identity snapshot in generation request payload",
                    details={"reason": f"{key} must be a list", "in": kind},
                )
            values[key] = tuple(value)
        else:
            values[key] = value
    try:
        return child_cls(**values)
    except TypeError as exc:
        raise ValidationFailedError(
            "malformed identity snapshot in generation request payload",
            details={"reason": str(exc), "in": kind},
        ) from exc
