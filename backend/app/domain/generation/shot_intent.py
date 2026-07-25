"""Shot intent — the cinematic vocabulary of Planner V2 (α8.7).

This module is the *Knowledge/Intent* plane's new first-class concept: a shot is
no longer just a description + seed, it is a piece of **structured cinematic
intent**. The Planner decides the intent (``what`` the shot is); the Prompt
Builder later decides ``how`` to express it to a generator. Provider/render
wording (``photorealistic``, ``8k``, ``SDXL`` …) never appears here — see the
``CINEMATIC_STORYBOARD_CONTRACT.md`` invariant CS-8.

Everything here is a pure, immutable value object with small, controlled
vocabularies (CS-A). ``StoryArcTemplate``\\ s are *data* (CS-3 / Q2): the shape of a
story is a template the planner selects and instantiates, so new arc kinds
(interview, tutorial, trailer …) slot in without changing planner logic.

Nothing in this module performs I/O, chooses providers, or emits prompt text.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from itertools import pairwise
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence


class ShotType(StrEnum):
    """The taxonomy slot / framing scale of a shot (CS-7 primary dimension)."""

    ESTABLISHING = "establishing"
    WIDE = "wide"
    MEDIUM = "medium"
    CLOSE_UP = "close_up"
    DETAIL = "detail"
    ACTION = "action"
    ENDING = "ending"


class Camera(StrEnum):
    """How the **camera** behaves — deliberately distinct from subject movement."""

    STATIC = "static"
    PUSH_IN = "push_in"
    PULL_BACK = "pull_back"
    PAN_LEFT = "pan_left"
    PAN_RIGHT = "pan_right"
    TRACK = "track"
    CRANE = "crane"
    HANDHELD = "handheld"


class Movement(StrEnum):
    """What the **subject** does — a different concept from the camera's motion."""

    STILL = "still"
    WALKING = "walking"
    RUNNING = "running"
    TURNING = "turning"
    LOOKING = "looking"
    INTERACTING = "interacting"


class Transition(StrEnum):
    """The edit intent from the previous shot (planning intent; not yet rendered)."""

    CUT = "cut"
    DISSOLVE = "dissolve"
    FADE = "fade"
    MATCH_CUT = "match_cut"


class FocusHint(StrEnum):
    """A template's abstract focus, resolved to a concrete identity id by the planner.

    The template says *what kind of thing* the shot centres on; the planner maps it
    to an actual character/location/prop id from the project's Identity Runtime.
    """

    SUBJECT = "subject"  # the primary character
    ENVIRONMENT = "environment"  # the primary location
    DETAIL = "detail"  # a prop / texture insert


@dataclass(frozen=True, slots=True)
class ShotIntent:
    """Structured cinematic intent for a single shot (Planner V2 output).

    CS-7 splits the dimensions into **primary** (framing/composition/camera/subject)
    and **secondary** (motion/meaning/edit). Adjacent shots must differ in at least
    one primary *and* at least one secondary dimension.
    """

    shot_type: ShotType
    camera: Camera
    movement: Movement
    subject_focus: str | None = None
    emotional_purpose: str = ""
    transition_from_previous: Transition | None = None

    def primary_signature(self) -> tuple[object, ...]:
        """Framing scale, camera behaviour, and focal subject (CS-7 primary)."""
        return (self.shot_type, self.camera, self.subject_focus)

    def secondary_signature(self) -> tuple[object, ...]:
        """Subject movement, narrative purpose, and edit intent (CS-7 secondary)."""
        return (self.movement, self.emotional_purpose, self.transition_from_previous)

    def signature(self) -> tuple[object, ...]:
        """The full identity of the intent — two intents are equal iff these match."""
        return self.primary_signature() + self.secondary_signature()


def differs_primary(a: ShotIntent, b: ShotIntent) -> bool:
    """True when the two intents differ in ≥1 *primary* cinematic dimension."""
    return a.primary_signature() != b.primary_signature()


def differs_secondary(a: ShotIntent, b: ShotIntent) -> bool:
    """True when the two intents differ in ≥1 *secondary* variation."""
    return a.secondary_signature() != b.secondary_signature()


def adjacent_ok(a: ShotIntent, b: ShotIntent) -> bool:
    """CS-7: adjacent shots must differ in ≥1 primary **and** ≥1 secondary dimension.

    Requiring both means two shots that differ *only* by ``transition`` (a
    secondary) are illegal — structurally preventing the α8.6 duplicate-scene defect.
    """
    return differs_primary(a, b) and differs_secondary(a, b)


# ---------------------------------------------------------------------------
# Story arc templates (data-driven — Q2 / CS-3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TemplateBeat:
    """One beat of a story arc: the cinematic defaults for a shot slot.

    The planner instantiates a ``ShotIntent`` from a beat, resolving ``focus`` to a
    concrete identity id. Beats carry *intent* only — never prompt/provider text.
    """

    shot_type: ShotType
    camera: Camera
    movement: Movement
    emotional_purpose: str
    focus: FocusHint = FocusHint.SUBJECT
    transition_from_previous: Transition | None = None


@dataclass(frozen=True, slots=True)
class StoryArcTemplate:
    """An ordered set of beats describing the shape of a story (CS-3)."""

    kind: str
    beats: tuple[TemplateBeat, ...]

    @property
    def shot_count(self) -> int:
        return len(self.beats)


# The initial ``cinematic`` arcs (CINEMATIC_STORYBOARD_CONTRACT.md §6). Authored as
# data so adjacent beats always satisfy CS-7; validated by unit tests.
_CINEMATIC_ARCS: dict[int, StoryArcTemplate] = {
    3: StoryArcTemplate(
        kind="cinematic",
        beats=(
            TemplateBeat(
                ShotType.ESTABLISHING,
                Camera.CRANE,
                Movement.STILL,
                "arrival",
                FocusHint.ENVIRONMENT,
            ),
            TemplateBeat(
                ShotType.MEDIUM,
                Camera.TRACK,
                Movement.WALKING,
                "focus",
                FocusHint.SUBJECT,
                Transition.CUT,
            ),
            TemplateBeat(
                ShotType.ENDING,
                Camera.PULL_BACK,
                Movement.STILL,
                "resolution",
                FocusHint.ENVIRONMENT,
                Transition.DISSOLVE,
            ),
        ),
    ),
    5: StoryArcTemplate(
        kind="cinematic",
        beats=(
            TemplateBeat(
                ShotType.ESTABLISHING,
                Camera.CRANE,
                Movement.STILL,
                "arrival",
                FocusHint.ENVIRONMENT,
            ),
            TemplateBeat(
                ShotType.MEDIUM,
                Camera.TRACK,
                Movement.WALKING,
                "exploration",
                FocusHint.SUBJECT,
                Transition.CUT,
            ),
            TemplateBeat(
                ShotType.CLOSE_UP,
                Camera.PUSH_IN,
                Movement.LOOKING,
                "intimacy",
                FocusHint.SUBJECT,
                Transition.DISSOLVE,
            ),
            TemplateBeat(
                ShotType.DETAIL,
                Camera.STATIC,
                Movement.STILL,
                "texture",
                FocusHint.DETAIL,
                Transition.CUT,
            ),
            TemplateBeat(
                ShotType.ENDING,
                Camera.PULL_BACK,
                Movement.TURNING,
                "resolution",
                FocusHint.ENVIRONMENT,
                Transition.DISSOLVE,
            ),
        ),
    ),
    6: StoryArcTemplate(
        kind="cinematic",
        beats=(
            TemplateBeat(
                ShotType.ESTABLISHING,
                Camera.CRANE,
                Movement.STILL,
                "arrival",
                FocusHint.ENVIRONMENT,
            ),
            TemplateBeat(
                ShotType.WIDE,
                Camera.PAN_RIGHT,
                Movement.WALKING,
                "exploration",
                FocusHint.SUBJECT,
                Transition.CUT,
            ),
            TemplateBeat(
                ShotType.MEDIUM,
                Camera.TRACK,
                Movement.WALKING,
                "journey",
                FocusHint.SUBJECT,
                Transition.CUT,
            ),
            TemplateBeat(
                ShotType.CLOSE_UP,
                Camera.PUSH_IN,
                Movement.LOOKING,
                "intimacy",
                FocusHint.SUBJECT,
                Transition.DISSOLVE,
            ),
            TemplateBeat(
                ShotType.ACTION,
                Camera.HANDHELD,
                Movement.RUNNING,
                "tension",
                FocusHint.SUBJECT,
                Transition.CUT,
            ),
            TemplateBeat(
                ShotType.ENDING,
                Camera.PULL_BACK,
                Movement.STILL,
                "resolution",
                FocusHint.ENVIRONMENT,
                Transition.DISSOLVE,
            ),
        ),
    ),
}

# Pools used to synthesise an arc for shot counts outside the authored set. Ordered
# so consecutive indices always yield a different camera (guaranteeing a primary
# change) and a unique per-beat purpose guarantees a secondary change (CS-7).
_MIDDLE_TYPES: tuple[ShotType, ...] = (
    ShotType.WIDE,
    ShotType.MEDIUM,
    ShotType.CLOSE_UP,
    ShotType.DETAIL,
    ShotType.ACTION,
)
_CAMERAS: tuple[Camera, ...] = (
    Camera.PAN_RIGHT,
    Camera.TRACK,
    Camera.PUSH_IN,
    Camera.HANDHELD,
    Camera.PAN_LEFT,
    Camera.CRANE,
)
_MOVEMENTS: tuple[Movement, ...] = (
    Movement.WALKING,
    Movement.LOOKING,
    Movement.RUNNING,
    Movement.TURNING,
    Movement.INTERACTING,
)
_TRANSITIONS: tuple[Transition, ...] = (Transition.CUT, Transition.DISSOLVE, Transition.MATCH_CUT)

_ESTABLISHING_BEAT = TemplateBeat(
    ShotType.ESTABLISHING, Camera.CRANE, Movement.STILL, "arrival", FocusHint.ENVIRONMENT
)


def _synthesize_arc(shot_count: int) -> StoryArcTemplate:
    """Deterministically build a CS-7-satisfying arc for any ``shot_count`` ≥ 1.

    α8.7 targets the authored 3/5/6 arcs; this keeps the planner robust for any
    duration the caller requests without ever emitting a repeated-scene storyboard.
    """
    if shot_count <= 1:
        return StoryArcTemplate(kind="cinematic", beats=(_ESTABLISHING_BEAT,))

    beats: list[TemplateBeat] = [_ESTABLISHING_BEAT]
    for i in range(shot_count - 2):
        beats.append(
            TemplateBeat(
                shot_type=_MIDDLE_TYPES[i % len(_MIDDLE_TYPES)],
                camera=_CAMERAS[i % len(_CAMERAS)],
                movement=_MOVEMENTS[i % len(_MOVEMENTS)],
                emotional_purpose=f"beat {i + 1}",
                focus=FocusHint.SUBJECT,
                transition_from_previous=_TRANSITIONS[i % len(_TRANSITIONS)],
            )
        )
    beats.append(
        TemplateBeat(
            ShotType.ENDING,
            Camera.PULL_BACK,
            Movement.STILL,
            "resolution",
            FocusHint.ENVIRONMENT,
            Transition.DISSOLVE,
        )
    )
    return StoryArcTemplate(kind="cinematic", beats=tuple(beats))


def select_arc(shot_count: int, *, kind: str = "cinematic") -> StoryArcTemplate:
    """Select the story arc template for ``shot_count`` shots.

    Returns an authored arc for the well-known 3/5/6 counts; otherwise synthesises a
    deterministic CS-7-satisfying arc. Only the ``cinematic`` kind exists in α8.7;
    other kinds are enabled by this data-driven design but out of scope for now.
    """
    if kind != "cinematic":
        raise ValueError(f"unknown story arc kind: {kind!r}")
    if shot_count < 1:
        raise ValueError("shot_count must be >= 1")
    return _CINEMATIC_ARCS.get(shot_count) or _synthesize_arc(shot_count)


# ---------------------------------------------------------------------------
# Deterministic, position-independent shot ids + seeds (CS-4 / Q4)
# ---------------------------------------------------------------------------

_SEED_BITS = 63  # non-negative and always fits a signed 64-bit (bigint) column


def shot_id_for(shot_type: ShotType, *, scene: int = 1, occurrence: int = 1) -> str:
    """A stable, semantic shot id — e.g. ``scene-001-establishing``.

    The id is derived from *meaning* (scene + shot type), never array position, so
    it stays stable when another shot is inserted or the arc is reshaped. A repeated
    shot type within one arc gets a 1-based ``occurrence`` suffix; authored arcs use
    unique shot types, so their ids carry no suffix.
    """
    slug = shot_type.value.replace("_", "")
    base = f"scene-{scene:03d}-{slug}"
    return base if occurrence == 1 else f"{base}-{occurrence}"


def assign_shot_ids(beats: Sequence[TemplateBeat], *, scene: int = 1) -> tuple[str, ...]:
    """Assign a stable shot id to each beat, disambiguating repeated shot types."""
    counts: dict[ShotType, int] = {}
    ids: list[str] = []
    for beat in beats:
        counts[beat.shot_type] = counts.get(beat.shot_type, 0) + 1
        ids.append(shot_id_for(beat.shot_type, scene=scene, occurrence=counts[beat.shot_type]))
    return tuple(ids)


def derive_shot_seed(project_seed: int, shot_id: str) -> int:
    """``shot_seed = H(project_seed, shot_id)`` — deterministic per-shot seed (CS-4).

    Uses blake2b over ``"{project_seed}|{shot_id}"`` and keeps the low 63 bits, so
    the value is reproducible, non-negative, and fits a signed 64-bit column. Hashing
    (not ``project_seed + index``) keeps a shot's seed stable under reordering because
    the seed follows the *identity* of the shot, not its position.
    """
    digest = hashlib.blake2b(f"{project_seed}|{shot_id}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") & ((1 << _SEED_BITS) - 1)


# ---------------------------------------------------------------------------
# Invariants: CS-7 (cinematic continuity) and CS-8 (semantic-intent boundary)
# ---------------------------------------------------------------------------


class CinematicContinuityError(ValueError):
    """Raised when adjacent shots fail CS-7 (a duplicate-scene storyboard)."""


class ProviderLanguageError(ValueError):
    """Raised when planner intent leaks provider/render language (CS-8)."""


# Provider/render vocabulary that must never appear in *planner* output (CS-8). This
# is generator-facing wording — it belongs exclusively to the Prompt Builder. The
# planner speaks in cinematic *intent* (``CLOSE_UP``), not render adjectives.
PROVIDER_LEXICON: frozenset[str] = frozenset(
    {
        "photorealistic",
        "hyperrealistic",
        "8k",
        "4k",
        "ultra detailed",
        "ultra-detailed",
        "highly detailed",
        "masterpiece",
        "best quality",
        "cinematic lighting",
        "octane render",
        "unreal engine",
        "trending on artstation",
        "sdxl",
        "flux",
        "comfyui",
        "midjourney",
        "dall-e",
        "dalle",
        "stable diffusion",
    }
)


def provider_language_in(*texts: str) -> tuple[str, ...]:
    """Return any banned provider/render terms found across ``texts`` (lower-cased)."""
    haystack = " ".join(t.lower() for t in texts if t)
    return tuple(term for term in sorted(PROVIDER_LEXICON) if term in haystack)


def validate_adjacency(intents: Sequence[ShotIntent]) -> None:
    """Enforce CS-7 across a storyboard: every adjacent pair differs cinematically.

    Raises ``CinematicContinuityError`` on the first offending pair. A single-shot
    (or empty) storyboard trivially satisfies the invariant.
    """
    for index, (a, b) in enumerate(pairwise(intents), start=1):
        if not adjacent_ok(a, b):
            reason = "primary" if not differs_primary(a, b) else "secondary"
            raise CinematicContinuityError(
                f"CS-7 violated: shots {index - 1}->{index} do not differ in a "
                f"{reason} cinematic dimension"
            )


def assert_semantic_only(intents: Sequence[ShotIntent]) -> None:
    """Enforce CS-8: planner-authored intent carries no provider/render language.

    Only the planner-authored free-text field (``emotional_purpose``) is checked;
    ``subject_focus`` is an identity id and the shot ``description`` is user-supplied
    pass-through, so neither is the planner *adding* provider wording.
    """
    for index, intent in enumerate(intents):
        hits = provider_language_in(intent.emotional_purpose)
        if hits:
            raise ProviderLanguageError(
                f"CS-8 violated: shot {index} intent contains provider language {hits}"
            )


# ---------------------------------------------------------------------------
# Introspection helper (test-facing; never wired into runtime scoring)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StoryboardDiversityReport:
    """A pure, objective measure of a storyboard's cinematic diversity.

    Not a runtime score and never consulted by the pipeline — a diagnostic value
    object so planner tests can assert diversity (``duplicate_intents == 0``,
    ``unique_shot_types >= 3``, ``satisfies_cs7``) without any generator or ffmpeg.
    """

    template_used: str
    shot_count: int
    primary_changes: int
    secondary_changes: int
    duplicate_intents: int
    unique_shot_types: int
    camera_variety: int
    satisfies_cs7: bool

    @classmethod
    def from_intents(
        cls, intents: tuple[ShotIntent, ...], *, template_used: str = ""
    ) -> StoryboardDiversityReport:
        pairs = tuple(pairwise(intents))
        primary_changes = sum(1 for a, b in pairs if differs_primary(a, b))
        secondary_changes = sum(1 for a, b in pairs if differs_secondary(a, b))
        unique_signatures = {i.signature() for i in intents}
        return cls(
            template_used=template_used,
            shot_count=len(intents),
            primary_changes=primary_changes,
            secondary_changes=secondary_changes,
            duplicate_intents=len(intents) - len(unique_signatures),
            unique_shot_types=len({i.shot_type for i in intents}),
            camera_variety=len({i.camera for i in intents}),
            satisfies_cs7=all(adjacent_ok(a, b) for a, b in pairs),
        )
