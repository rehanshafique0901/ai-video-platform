"""Identity Runtime — the persistent *world state* of a generation project.

The Identity Runtime is far more than a prompt suffix: it is the single source of
truth for *who and what* exists across an entire video — characters (with their
stable appearance, clothing, accessories, expression/pose catalogues, voice and
reference images), locations, recurring props, and the project-wide look (camera,
lighting, colour palette, global art style, plus music/subtitle style carried for
later slices). A stable ``seed`` biases the generator toward visual consistency.

Every generation flows Identity Runtime -> Prompt Builder -> Generator (see
``prompt_builder``); a shot never prompts from scratch. That is what stops faces,
clothing, and scenes drifting across a long video.

Everything here is a pure, immutable value object with deterministic
serialisation — no I/O, no embeddings computed here (reference *images* are held
as storage refs; derived embeddings belong to a later runtime identity-state).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class GlobalStyle(StrEnum):
    """Project-wide art direction applied to every shot."""

    PIXAR = "pixar"
    DISNEY = "disney"
    ANIME = "anime"
    REALISTIC = "realistic"
    WATERCOLOR = "watercolor"
    CLAYMATION = "claymation"

    def prompt_fragment(self) -> str:
        return f"{self.value} style"


def join_fragments(parts: tuple[str, ...]) -> str:
    """Join non-empty, stripped fragments with ', ' — deterministic and stable."""
    return ", ".join(p.strip() for p in parts if p and p.strip())


@dataclass(frozen=True, slots=True)
class Character:
    """A consistent character carried, unchanged, into every shot it appears in.

    ``appearance`` holds the *stable* visual identity (face, hair, build).
    ``expressions`` / ``poses`` are catalogues a shot may draw from — they are
    per-shot variation, so they are NOT part of the stable identity fragment.
    ``reference_image_refs`` are storage keys of anchor images for future
    image-conditioned generation; ``voice`` / ``personality`` feed later
    narration / lip-sync slices and never enter the image prompt.
    """

    id: str
    name: str
    age: str | None = None
    appearance: tuple[str, ...] = ()
    clothing: str | None = None
    accessories: tuple[str, ...] = ()
    expressions: tuple[str, ...] = ()
    poses: tuple[str, ...] = ()
    voice: str | None = None
    personality: str | None = None
    reference_image_refs: tuple[str, ...] = ()

    def prompt_fragment(self) -> str:
        parts: list[str] = []
        if self.age:
            parts.append(self.age)
        parts.extend(self.appearance)
        if self.clothing:
            parts.append(f"wearing {self.clothing}")
        if self.accessories:
            parts.append("with " + ", ".join(a.strip() for a in self.accessories if a.strip()))
        detail = join_fragments(tuple(parts))
        return f"{self.name} ({detail})" if detail else self.name


@dataclass(frozen=True, slots=True)
class Location:
    """A recurring place (a room, a park) kept consistent across shots."""

    id: str
    name: str
    descriptors: tuple[str, ...] = ()

    def prompt_fragment(self) -> str:
        detail = join_fragments(self.descriptors)
        return f"{self.name} ({detail})" if detail else self.name


@dataclass(frozen=True, slots=True)
class Prop:
    """A recurring object that must stay consistent (teddy bear, toy car, balloon)."""

    id: str
    name: str
    descriptors: tuple[str, ...] = ()

    def prompt_fragment(self) -> str:
        detail = join_fragments(self.descriptors)
        return f"{self.name} ({detail})" if detail else self.name


@dataclass(frozen=True, slots=True)
class IdentityProfile:
    """Immutable project world state that every shot references.

    ``seed`` is the stable seed reused across shots for consistency; repair may
    derive a *new* seed to escape a bad generation, but this profile never
    changes. ``music_style`` / ``subtitle_style`` are carried for later audio /
    subtitle slices and are intentionally excluded from the image prompt.
    """

    seed: int
    global_style: GlobalStyle = GlobalStyle.PIXAR
    characters: tuple[Character, ...] = ()
    locations: tuple[Location, ...] = ()
    props: tuple[Prop, ...] = ()
    camera_style: str | None = None
    lighting: str | None = None
    color_palette: str | None = None
    music_style: str | None = None
    subtitle_style: str | None = None

    def character(self, character_id: str) -> Character | None:
        for character in self.characters:
            if character.id == character_id:
                return character
        return None

    def location(self, location_id: str) -> Location | None:
        for location in self.locations:
            if location.id == location_id:
                return location
        return None
