"""Unit tests for the Pillow feature extractor (real in-memory images)."""

from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image

from app.infrastructure.generation.pillow_feature_extractor import PillowFeatureExtractor

pytestmark = pytest.mark.unit


def _png(img: Image.Image) -> bytes:
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _solid(w: int, h: int, colour: tuple[int, int, int]) -> bytes:
    return _png(Image.new("RGB", (w, h), colour))


def _gradient(w: int, h: int) -> bytes:
    img = Image.new("RGB", (w, h))
    px = img.load()
    assert px is not None
    for x in range(w):
        for y in range(h):
            px[x, y] = (x % 256, y % 256, (x * y) % 256)
    return _png(img)


async def test_reports_dimensions_and_aspect_ratio() -> None:
    obs = await PillowFeatureExtractor().extract(_solid(640, 480, (10, 20, 30)))
    assert obs.produced
    assert obs.width == 640 and obs.height == 480
    assert obs.aspect_ratio == pytest.approx(640 / 480)


async def test_solid_image_is_blank() -> None:
    obs = await PillowFeatureExtractor().extract(_solid(128, 128, (127, 127, 127)))
    assert obs.is_blank is True
    assert obs.blank_probability is not None and obs.blank_probability > 0.5


async def test_gradient_image_is_not_blank_and_has_features() -> None:
    obs = await PillowFeatureExtractor().extract(_gradient(128, 128))
    assert obs.is_blank is False
    assert obs.perceptual_hash is not None and len(obs.perceptual_hash) == 16
    assert obs.colour_histogram is not None and len(obs.colour_histogram) == 24
    assert obs.edge_density is not None and obs.edge_density > 0
    assert 0.0 <= obs.average_brightness <= 1.0  # type: ignore[operator]
    assert 0.0 <= obs.average_saturation <= 1.0  # type: ignore[operator]


async def test_identical_images_are_maximally_similar() -> None:
    img = _gradient(128, 128)
    obs = await PillowFeatureExtractor().extract(img, reference=img)
    assert obs.similarity_to_reference == pytest.approx(1.0)


async def test_different_images_are_less_similar() -> None:
    a = _gradient(128, 128)
    b = _solid(128, 128, (0, 0, 0))
    obs = await PillowFeatureExtractor().extract(a, reference=b)
    assert obs.similarity_to_reference is not None
    assert obs.similarity_to_reference < 1.0


async def test_first_shot_has_no_similarity() -> None:
    obs = await PillowFeatureExtractor().extract(_gradient(64, 64))
    assert obs.similarity_to_reference is None


async def test_undecodable_bytes_report_not_produced() -> None:
    obs = await PillowFeatureExtractor().extract(b"not-an-image")
    assert obs.produced is False
    assert obs.width is None
