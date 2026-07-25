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


def build_prompt(
    identity: IdentityProfile,
    *,
    description: str,
    character_ids: tuple[str, ...] = (),
    location_id: str | None = None,
    modifiers: tuple[str, ...] = (),
) -> str:
    """Build the full prompt for one shot.

    Order: the shot description, then the named characters, the location, the
    recurring props, and finally the project look (camera, lighting, colour
    palette, global style). ``modifiers`` are optional per-shot additions (e.g. a
    chosen expression or pose). Music/subtitle style are deliberately omitted —
    they belong to later slices, not the image prompt.
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

    fragments.extend(modifiers)

    if identity.camera_style:
        fragments.append(identity.camera_style)
    if identity.lighting:
        fragments.append(identity.lighting)
    if identity.color_palette:
        fragments.append(identity.color_palette)
    fragments.append(identity.global_style.prompt_fragment())

    return join_fragments(tuple(fragments))
