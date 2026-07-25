"""Pillow image feature extractor (implements ``IImageFeatureExtractor``).

Turns raw image bytes into an :class:`ObservedImage` of cheap, model-free features
computed with Pillow only (no numpy). The verification *policy* stays pure and
byte-free; this adapter is the measuring side. Richer fields (brightness,
saturation, perceptual hash, colour histogram, edge density, blank/blur/watermark
scores) are produced from day one — they are inexpensive and become valuable
inputs for future verification/repair, and the perceptual hash backs both
cross-frame similarity and the timeline duplicate check.

Heavier ML features (CLIP / face embeddings) attach later as additional fields
without changing this adapter's contract.
"""

from __future__ import annotations

import asyncio
from io import BytesIO

from PIL import Image, ImageFilter, ImageStat

from app.application.interfaces.image_feature_extractor import IImageFeatureExtractor
from app.domain.generation.verification import ObservedImage

# Grayscale stddev (0..255) at/above which an image is clearly not blank.
_BLANK_STDDEV_FULL = 12.0
# Edge stddev (0..255) at/above which an image is clearly sharp (not blurry).
_SHARP_STDDEV_FULL = 40.0
_HIST_BINS = 8  # per channel -> 24-int coarse colour histogram
_THUMB = 64  # work on a small thumbnail for speed + determinism


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _ahash(gray: Image.Image) -> str:
    """64-bit average hash as 16 hex chars (deterministic perceptual hash)."""
    small = gray.resize((8, 8), Image.Resampling.LANCZOS)
    pixels = list(small.getdata())
    avg = sum(pixels) / len(pixels)
    bits = 0
    for px in pixels:
        bits = (bits << 1) | (1 if px >= avg else 0)
    return f"{bits:016x}"


def _hamming_similarity(a: str, b: str) -> float:
    try:
        distance = bin(int(a, 16) ^ int(b, 16)).count("1")
    except ValueError:
        return 0.0
    return _clamp01(1.0 - distance / 64.0)


def _coarse_histogram(rgb: Image.Image) -> tuple[int, ...]:
    """A 24-int histogram: ``_HIST_BINS`` buckets per R/G/B channel."""
    full = rgb.histogram()  # 768 values: R[0..255], G[0..255], B[0..255]
    step = 256 // _HIST_BINS
    bins: list[int] = []
    for channel in range(3):
        base = channel * 256
        for b in range(_HIST_BINS):
            start = base + b * step
            bins.append(sum(full[start : start + step]))
    return tuple(bins)


def _watermark_probability(gray: Image.Image, edges: Image.Image) -> float:
    """Naive heuristic: watermarks usually add busy detail in a bottom corner.

    Compares edge density in the bottom-right strip to the overall image; a large
    excess suggests overlaid text/logo. Intentionally conservative — a real
    detector plugs in later without changing the contract.
    """
    w, h = edges.size
    if w < 8 or h < 8:
        return 0.0
    strip = edges.crop((w // 2, int(h * 0.85), w, h))
    strip_density = ImageStat.Stat(strip).mean[0]
    overall_density = ImageStat.Stat(edges).mean[0] or 1.0
    excess = (strip_density / overall_density) - 1.0
    return _clamp01(excess / 3.0)


def _extract_sync(image: bytes, reference: bytes | None) -> ObservedImage:
    try:
        img = Image.open(BytesIO(image))
        img.load()
    except Exception:
        return ObservedImage(produced=False)

    width, height = img.size
    rgb = img.convert("RGB")
    thumb = rgb.resize((_THUMB, _THUMB), Image.Resampling.LANCZOS)
    gray = thumb.convert("L")

    gray_stat = ImageStat.Stat(gray)
    brightness = gray_stat.mean[0] / 255.0
    gray_stddev = gray_stat.stddev[0]

    hsv_stat = ImageStat.Stat(thumb.convert("HSV"))
    saturation = hsv_stat.mean[1] / 255.0

    edges = gray.filter(ImageFilter.FIND_EDGES)
    edge_stat = ImageStat.Stat(edges)
    edge_density = edge_stat.mean[0] / 255.0
    sharpness = _clamp01(edge_stat.stddev[0] / _SHARP_STDDEV_FULL)

    blank_probability = _clamp01(1.0 - gray_stddev / _BLANK_STDDEV_FULL)
    watermark_probability = _watermark_probability(gray, edges)
    phash = _ahash(gray)
    similarity = None
    if reference is not None:
        try:
            ref_img = Image.open(BytesIO(reference))
            ref_img.load()
            ref_gray = ref_img.convert("RGB").resize((_THUMB, _THUMB)).convert("L")
            similarity = _hamming_similarity(phash, _ahash(ref_gray))
        except Exception:
            similarity = None

    return ObservedImage(
        produced=True,
        width=width,
        height=height,
        is_blank=blank_probability >= 0.5,
        similarity_to_reference=similarity,
        has_watermark=watermark_probability >= 0.5,
        aspect_ratio=width / height if height else None,
        average_brightness=brightness,
        average_saturation=saturation,
        perceptual_hash=phash,
        colour_histogram=_coarse_histogram(thumb),
        edge_density=edge_density,
        blank_probability=blank_probability,
        blur_score=sharpness,
        watermark_probability=watermark_probability,
    )


class PillowFeatureExtractor(IImageFeatureExtractor):
    """Compute :class:`ObservedImage` features with Pillow (off the event loop)."""

    async def extract(self, image: bytes, *, reference: bytes | None = None) -> ObservedImage:
        return await asyncio.to_thread(_extract_sync, image, reference)
