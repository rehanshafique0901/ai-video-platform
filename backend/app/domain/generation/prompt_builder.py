"""Prompt Builder — compose a shot's final prompt from the Identity Runtime.

This is the seam the user insisted on: never ``prompt -> generate``, always
``Identity Runtime -> Prompt Builder -> Generator``. Given the world state and a
shot's intent (description, which characters, which location, optional per-shot
expression/pose), it deterministically builds the exact text a generator
receives, anchoring every shot to the same identity so the look stays consistent.

Pure and deterministic; contains no provider or execution logic.
"""

from __future__ import annotations

from app.domain.generation.identity import IdentityProfile, join_fragments
from app.domain.generation.shot_intent import Camera, Movement, ShotIntent, ShotType

# The one place cinematic *intent* becomes generator-facing *language* (CS-8): the
# planner chooses enum values; only here are they translated into words. Kept as
# plain, provider-neutral cinematic English — no tool names, no "8k/masterpiece".
_SHOT_TYPE_PHRASES: dict[ShotType, str] = {
    ShotType.ESTABLISHING: "establishing shot",
    ShotType.WIDE: "wide shot",
    ShotType.MEDIUM: "medium shot",
    ShotType.CLOSE_UP: "close-up shot",
    ShotType.DETAIL: "detail shot",
    ShotType.ACTION: "dynamic action shot",
    ShotType.ENDING: "closing shot",
}

_CAMERA_PHRASES: dict[Camera, str] = {
    Camera.STATIC: "static camera",
    Camera.PUSH_IN: "slow push-in",
    Camera.PULL_BACK: "camera pulling back",
    Camera.PAN_LEFT: "camera panning left",
    Camera.PAN_RIGHT: "camera panning right",
    Camera.TRACK: "tracking shot",
    Camera.CRANE: "sweeping crane shot",
    Camera.HANDHELD: "handheld camera",
}

# Subject movement (STILL contributes no phrase to avoid noise).
_MOVEMENT_PHRASES: dict[Movement, str] = {
    Movement.WALKING: "walking",
    Movement.RUNNING: "running",
    Movement.TURNING: "turning",
    Movement.LOOKING: "looking around",
    Movement.INTERACTING: "interacting",
}


def intent_phrases(intent: ShotIntent) -> tuple[str, ...]:
    """Translate a ``ShotIntent``'s visual dimensions into descriptor phrases.

    Only the dimensions that shape the *image* are expressed — framing (shot type),
    camera behaviour, and subject movement. ``transition_from_previous`` is an *edit*
    intent (it does not alter a single frame) and ``emotional_purpose`` is narrative
    metadata; both are carried in provenance, not in the image prompt.
    """
    parts = [_SHOT_TYPE_PHRASES[intent.shot_type], _CAMERA_PHRASES[intent.camera]]
    movement = _MOVEMENT_PHRASES.get(intent.movement)
    if movement:
        parts.append(movement)
    return tuple(parts)


def build_prompt(
    identity: IdentityProfile,
    *,
    description: str,
    character_ids: tuple[str, ...] = (),
    location_id: str | None = None,
    modifiers: tuple[str, ...] = (),
    intent: ShotIntent | None = None,
) -> str:
    """Build the full prompt for one shot.

    Order: the shot description, then the named characters, the location, the
    recurring props, the per-shot cinematic ``intent`` phrases (framing / camera /
    movement), any extra ``modifiers``, and finally the project look (camera,
    lighting, colour palette, global style). Music/subtitle style are deliberately
    omitted — they belong to later slices, not the image prompt.
    """
    fragments: list[str] = [description.strip()]

    for cid in character_ids:
        character = identity.character(cid)
        if character is not None:
            fragments.append(character.prompt_fragment())

    if location_id is not None:
        location = identity.location(location_id)
        if location is not None:
            fragments.append(location.prompt_fragment())

    for prop in identity.props:
        fragments.append(prop.prompt_fragment())

    if intent is not None:
        fragments.extend(intent_phrases(intent))

    fragments.extend(modifiers)

    if identity.camera_style:
        fragments.append(identity.camera_style)
    if identity.lighting:
        fragments.append(identity.lighting)
    if identity.color_palette:
        fragments.append(identity.color_palette)
    fragments.append(identity.global_style.prompt_fragment())

    return join_fragments(tuple(fragments))
