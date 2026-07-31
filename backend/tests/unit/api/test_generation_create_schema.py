"""Unit tests — the α10.0 create request keeps "unstated" distinguishable from "default".

`seed` and `global_style` are the two request fields a named world also declares. Ingress can
only apply the world's value where it can tell that the caller stated none, so both arrive as
`None` when omitted rather than as the platform default (ADR-0055 D4, IDENT-2). A default
baked into the DTO would silently outrank every authored world.
"""

from __future__ import annotations

import pytest

from app.api.v1.schemas.generations import GenerationCreateRequest
from app.domain.generation.identity import GlobalStyle

pytestmark = pytest.mark.unit


def test_an_unstated_seed_and_style_are_absent_rather_than_defaulted() -> None:
    body = GenerationCreateRequest(prompt="a paper boat")

    assert body.seed is None
    assert body.global_style is None


def test_a_stated_style_is_carried_verbatim() -> None:
    body = GenerationCreateRequest(prompt="a paper boat", global_style=GlobalStyle.CLAYMATION)

    assert body.global_style is GlobalStyle.CLAYMATION
