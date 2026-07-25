"""Identity Runtime — everything-consistency for a generation project.

The single biggest cause of "different face every frame" is that each generation
starts from scratch. The Identity Runtime fixes that by holding one immutable
description of *who and what* appears — characters, the scene, recurring objects,
the global art style — plus a **stable seed**. Every shot prompt is composed by
appending this identity's deterministic fragment, so the generator is always
extending the same identity rather than inventing a new one.

Everything here is a pure value object: no I/O, deterministic serialisation.
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


def _join(parts: tuple[str, ...]) -> str:
    """Join non-empty, stripped fragments with ', ' — deterministic and stable."""
    return ", ".join(p.strip() for p in parts if p and p.strip())


@dataclass(frozen=True, slots=True)
class Character:
    """A consistent character: identity descriptors carried into every shot.

    ``descriptors`` is an ordered tuple of appearance facts (face, age, hairstyle,
    clothing, accessories). ``voice`` / ``personality`` are carried for downstream
    narration/lip-sync slices and do not affect the image prompt.
    """

    id: str
    name: str
    descriptors: tuple[str, ...] = ()
    voice: str | None = None
    personality: str | None = None

    def prompt_fragment(self) -> str:
        descriptors = _join(self.descriptors)
        return f"{self.name} ({descriptors})" if descriptors else self.name


@dataclass(frozen=True, slots=True)
class SceneStyle:
    """The recurring environment: setting, lighting, weather, camera."""

    setting: str | None = None
    lighting: str | None = None
    weather: str | None = None
    camera: str | None = None

    def prompt_fragment(self) -> str:
        return _join(
            (self.setting or "", self.lighting or "", self.weather or "", self.camera or "")
        )


@dataclass(frozen=True, slots=True)
class ObjectAsset:
    """A recurring prop that must stay consistent across shots (teddy bear, toy car)."""

    id: str
    name: str
    descriptors: tuple[str, ...] = ()

    def prompt_fragment(self) -> str:
        descriptors = _join(self.descriptors)
        return f"{self.name} ({descriptors})" if descriptors else self.name


@dataclass(frozen=True, slots=True)
class IdentityProfile:
    """Immutable identity a whole project references.

    ``seed`` is the stable random seed reused across shots to bias the generator
    toward visual consistency. Repair may derive a *new* seed to escape a bad
    generation, but the profile's seed itself never changes.
    """

    seed: int
    global_style: GlobalStyle = GlobalStyle.PIXAR
    characters: tuple[Character, ...] = ()
    scene: SceneStyle | None = None
    objects: tuple[ObjectAsset, ...] = ()

    def character(self, character_id: str) -> Character | None:
        for character in self.characters:
            if character.id == character_id:
                return character
        return None

    def style_suffix(self, *, character_ids: tuple[str, ...] = ()) -> str:
        """Deterministic identity fragment appended to a shot's prompt.

        Includes only the characters named in ``character_ids`` (a shot rarely
        features everyone), then the scene, recurring objects, and finally the
        global style. Given the same profile + ids the output is byte-identical.
        """
        fragments: list[str] = []
        for cid in character_ids:
            character = self.character(cid)
            if character is not None:
                fragments.append(character.prompt_fragment())
        if self.scene is not None:
            scene_fragment = self.scene.prompt_fragment()
            if scene_fragment:
                fragments.append(scene_fragment)
        for obj in self.objects:
            fragments.append(obj.prompt_fragment())
        fragments.append(self.global_style.prompt_fragment())
        return _join(tuple(fragments))
