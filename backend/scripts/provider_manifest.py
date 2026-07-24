"""Pydantic schema + loaders for the α8.5c capability/provider design-time spec.

Three focused manifests under ``backend/providers/`` (design-time only; the
runtime never reads them — W8.5c.2, the DB is the runtime source of truth):

  * ``capabilities.yaml`` — the capability *vocabulary* (kind + typed I/O + params).
  * ``providers.yaml``    — providers, their adapters (the runtime-loadable unit)
                            with capability-specific ``supports`` constraints, and
                            model families.
  * ``routing.yaml``      — the routing *policy* (strategy per capability).

This module lives under ``scripts/`` — **not** under ``app/`` — so the runtime
cannot import it. The schema is strict (``extra="forbid"``) so a typo — or a
stray ``priority:`` key (R3 forbids integer priority) — fails loudly at load.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

# Coarse routing buckets — MUST mirror ``plugin_kind_enum`` / the code
# ``Capability`` enum. Extending this is a deliberate, migration-bearing act
# (deferred); the fine capability vocabulary is open by contrast.
KINDS: frozenset[str] = frozenset({"llm", "image", "video", "voice"})

# kinds for which a duration / resolution constraint is meaningful.
_DURATION_KINDS: frozenset[str] = frozenset({"video", "voice"})
_RESOLUTION_KINDS: frozenset[str] = frozenset({"image", "video"})

# α8.5d — kinds a feature-matrix entry is meaningful on, and video-only features.
_FEATURE_KINDS: frozenset[str] = frozenset({"image", "video"})
_VIDEO_ONLY_FEATURES: frozenset[str] = frozenset({"motion_control"})

# α8.5d — controlled output-format vocabulary per io type (Output Characteristics).
OUTPUT_FORMATS: dict[str, frozenset[str]] = {
    "image": frozenset({"png", "jpg", "jpeg", "webp", "gif"}),
    "video": frozenset({"mp4", "webm", "gif", "mov"}),
    "audio": frozenset({"wav", "mp3", "opus", "flac", "aac"}),
    "subtitle": frozenset({"srt", "vtt"}),
}


class Kind(StrEnum):
    LLM = "llm"
    IMAGE = "image"
    VIDEO = "video"
    VOICE = "voice"


class IOType(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    SUBTITLE = "subtitle"
    EMBEDDING = "embedding"


class Authentication(StrEnum):
    NONE = "none"
    API_KEY = "api_key"
    OAUTH = "oauth"
    TOKEN = "token"


class Pricing(StrEnum):
    FREE = "free"
    FREEMIUM = "freemium"
    PAID = "paid"


class AdapterStatus(StrEnum):
    PLANNED = "planned"
    IMPLEMENTED = "implemented"


class RoutingStrategy(StrEnum):
    FREE_FIRST = "free_first"
    LOWEST_COST = "lowest_cost"
    HIGHEST_QUALITY = "highest_quality"
    FASTEST = "fastest"
    BALANCED = "balanced"
    OFFLINE_ONLY = "offline_only"
    PRIVACY_FIRST = "privacy_first"
    COMMERCIAL_ONLY = "commercial_only"
    FREE_ONLY = "free_only"


class FallbackMode(StrEnum):
    AUTOMATIC = "automatic"
    NONE = "none"


class Selection(StrEnum):
    BEST_AVAILABLE = "best_available"
    FIRST_AVAILABLE = "first_available"


class Feature(StrEnum):
    """α8.5d — fine adapter *features* (not capabilities). Controlled vocabulary so
    provider capabilities aren't fragmented into dozens of tiny capabilities."""

    TXT2IMG = "txt2img"
    IMG2IMG = "img2img"
    NEGATIVE_PROMPT = "negative_prompt"
    SEED_CONTROL = "seed_control"
    REFERENCE_IMAGE = "reference_image"
    CONSISTENT_CHARACTER = "consistent_character"
    LORA = "lora"
    INPAINTING = "inpainting"
    OUTPAINTING = "outpainting"
    MOTION_CONTROL = "motion_control"
    FACE_REFERENCE = "face_reference"
    DEPTH_CONTROL = "depth_control"
    POSE_CONTROL = "pose_control"


class CostUnit(StrEnum):
    IMAGE = "image"
    SECOND = "second"
    MINUTE = "minute"
    TOKEN = "token"
    CHARACTER = "character"
    REQUEST = "request"


class CostSource(StrEnum):
    DECLARED = "declared"
    DERIVED = "derived"
    UNKNOWN = "unknown"


class DeviceBackend(StrEnum):
    METAL = "metal"
    CUDA = "cuda"
    ROCM = "rocm"
    CPU = "cpu"


class GenerationMode(StrEnum):
    QUICK = "quick"
    BALANCED = "balanced"
    QUALITY = "quality"
    ULTRA = "ultra"


# pricing tiers a free-first / free-only strategy can draw on.
FREE_PRICING: frozenset[str] = frozenset({Pricing.FREE, Pricing.FREEMIUM})

Score = Annotated[int, Field(ge=0, le=100)]
Limit = int | Literal["unlimited"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# capabilities.yaml
# --------------------------------------------------------------------------- #


class CapabilityDependencies(_Strict):
    """α8.5d — prerequisite *capabilities* (distinct from param-level requires/optional).

    ``requires`` forms an acyclic graph (validated); both default to empty.
    """

    requires: list[str] = Field(default_factory=list)
    optional: list[str] = Field(default_factory=list)


class CapabilityEntry(_Strict):
    id: str
    kind: Kind
    inputs: list[IOType]
    outputs: list[IOType]
    requires: list[str] = Field(default_factory=list)
    optional: list[str] = Field(default_factory=list)
    dependencies: CapabilityDependencies = Field(default_factory=CapabilityDependencies)


class Catalogue(_Strict):
    capabilities: list[CapabilityEntry]


# --------------------------------------------------------------------------- #
# providers.yaml
# --------------------------------------------------------------------------- #


class Quota(_Strict):
    daily: Limit | None = None
    monthly: Limit | None = None


class Scores(_Strict):
    quality: Score
    cost: Score
    speed: Score
    reliability: Score


class AdapterSupports(_Strict):
    """Capability-specific constraints the resolver can match against.

    All optional: an absent field means "unspecified", not "false".
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    commercial: bool | None = None
    nsfw: bool | None = None
    watermark: bool | None = None
    queue: bool | None = None
    asynchronous: bool | None = Field(default=None, alias="async")
    polling: bool | None = None
    webhook: bool | None = None
    max_duration_seconds: int | None = Field(default=None, gt=0)
    max_resolution: str | None = None


class Execution(_Strict):
    """α8.5d — where an adapter can run (AR7 local-first ordering reads this)."""

    local: bool = False
    cloud: bool = False


class Gpu(_Strict):
    metal: bool = False
    cuda: bool = False
    rocm: bool = False
    cpu: bool = False


class Hardware(_Strict):
    minimum_ram_gb: int | None = Field(default=None, gt=0)
    recommended_ram_gb: int | None = Field(default=None, gt=0)
    gpu: Gpu = Field(default_factory=Gpu)


class Estimated(_Strict):
    """α8.5d — resource estimation (scheduling / local execution / device selection)."""

    cold_start_seconds: float | None = Field(default=None, ge=0)
    warm_start_seconds: float | None = Field(default=None, ge=0)
    image_seconds: float | None = Field(default=None, ge=0)
    video_seconds: float | None = Field(default=None, ge=0)
    audio_seconds: float | None = Field(default=None, ge=0)
    peak_ram_gb: float | None = Field(default=None, ge=0)
    peak_vram_gb: float | None = Field(default=None, ge=0)
    disk_gb: float | None = Field(default=None, ge=0)


class AdapterRuntime(_Strict):
    execution: Execution = Field(default_factory=Execution)
    hardware: Hardware = Field(default_factory=Hardware)
    estimated: Estimated = Field(default_factory=Estimated)


class AdapterCost(_Strict):
    """α8.5d — declared cost hint (estimation-only; never a billing source, W8.5d.8)."""

    unit: CostUnit
    amount: float = Field(ge=0)
    currency: str = Field(min_length=3, max_length=3)
    source: CostSource = CostSource.DECLARED


class Adapter(_Strict):
    id: str
    capability: str
    status: AdapterStatus = AdapterStatus.PLANNED
    import_path: str | None = None
    fallback: list[str] = Field(default_factory=list)
    supports: AdapterSupports = Field(default_factory=AdapterSupports)
    features: list[Feature] = Field(default_factory=list)
    outputs: dict[str, list[str]] = Field(default_factory=dict)
    runtime: AdapterRuntime = Field(default_factory=AdapterRuntime)
    cost: AdapterCost | None = None


class Provider(_Strict):
    id: str
    name: str
    homepage: str | None = None
    documentation: str | None = None
    license: str | None = None
    commercial: bool = False
    authentication: Authentication = Authentication.NONE
    requires_login: bool = False
    pricing: Pricing
    quota: Quota = Field(default_factory=Quota)
    config_keys: list[str] = Field(default_factory=list)
    scores: Scores
    adapters: list[Adapter]


class Variant(_Strict):
    id: str
    provider: str


class Family(_Strict):
    id: str
    parent: str | None = None
    variants: list[Variant] = Field(default_factory=list)


class ProvidersDoc(_Strict):
    providers: list[Provider]
    families: list[Family] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# routing.yaml
# --------------------------------------------------------------------------- #


class RoutingDefaults(_Strict):
    strategy: RoutingStrategy
    fallback: FallbackMode
    selection: Selection


class RoutingPolicy(_Strict):
    strategy: RoutingStrategy | None = None
    fallback: FallbackMode | None = None
    selection: Selection | None = None


class RoutingDoc(_Strict):
    defaults: RoutingDefaults
    by_capability: dict[str, RoutingPolicy] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# devices.yaml (α8.5d — curated design-time device profiles; OPTIONAL manifest)
# --------------------------------------------------------------------------- #


class DeviceProfile(_Strict):
    id: str
    ram_gb: int | None = Field(default=None, gt=0)
    gpu: str | None = None
    backend: DeviceBackend
    unified_memory: bool = False
    preferred_mode: GenerationMode = GenerationMode.BALANCED


class DevicesDoc(_Strict):
    device_profiles: list[DeviceProfile]


# --------------------------------------------------------------------------- #
# Loaders
# --------------------------------------------------------------------------- #

PROVIDERS_DIR = Path(__file__).resolve().parent.parent / "providers"
CAPABILITIES_FILE = "capabilities.yaml"
PROVIDERS_FILE = "providers.yaml"
ROUTING_FILE = "routing.yaml"
DEVICES_FILE = "devices.yaml"  # α8.5d — OPTIONAL (absent ⇒ no device profiles)
MANIFEST_FILES = (CAPABILITIES_FILE, PROVIDERS_FILE, ROUTING_FILE)


def _read_yaml(path: Path) -> dict[str, object]:
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path} did not parse to a mapping")
    return data


def load_catalogue(path: Path) -> Catalogue:
    return Catalogue.model_validate(_read_yaml(path))


def load_providers(path: Path) -> ProvidersDoc:
    return ProvidersDoc.model_validate(_read_yaml(path))


def load_routing(path: Path) -> RoutingDoc:
    return RoutingDoc.model_validate(_read_yaml(path))


def load_devices(path: Path) -> DevicesDoc:
    return DevicesDoc.model_validate(_read_yaml(path))


def manifest_files_present(providers_dir: Path = PROVIDERS_DIR) -> list[bool]:
    return [(providers_dir / name).exists() for name in MANIFEST_FILES]
