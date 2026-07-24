"""Pydantic schema + loader for the α8.5c capability/provider design-time spec.

This module lives under ``scripts/`` — **not** under ``app/`` — on purpose: the
runtime must never read the YAML manifest (invariant **W8.5c.2**; the database is
the runtime source of truth). Only the offline tooling (the CI validator now, the
α8.5d seeder later) imports this.

The schema is intentionally strict (``extra="forbid"`` everywhere) so a typo — or
a stray ``priority:`` key (R3 forbids integer priority) — fails loudly at load
time rather than silently changing behaviour.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

# The coarse routing buckets — MUST mirror ``plugin_kind_enum`` / the code
# ``Capability`` enum. Extending this is a deliberate, migration-bearing act
# (deferred); the fine capability vocabulary is open by contrast.
KINDS: frozenset[str] = frozenset({"llm", "image", "video", "voice"})


class Kind(StrEnum):
    LLM = "llm"
    IMAGE = "image"
    VIDEO = "video"
    VOICE = "voice"


class Authentication(StrEnum):
    NONE = "none"
    API_KEY = "api_key"
    OAUTH = "oauth"
    TOKEN = "token"


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


Score = Annotated[int, Field(ge=0, le=100)]
Limit = int | Literal["unlimited"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# Catalogue
# --------------------------------------------------------------------------- #


class CapabilityEntry(_Strict):
    id: str
    kind: Kind


class Catalogue(_Strict):
    capabilities: list[CapabilityEntry]


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


class FreeTier(_Strict):
    available: bool
    signup_required: bool = False
    api_key_required: bool = False
    daily_limit: Limit = "unlimited"
    monthly_limit: Limit = "unlimited"
    watermark: bool = False


class Scores(_Strict):
    quality: Score
    cost: Score
    speed: Score
    reliability: Score


class Adapter(_Strict):
    id: str
    capability: str
    status: AdapterStatus = AdapterStatus.PLANNED
    import_path: str | None = None
    fallback: list[str] = Field(default_factory=list)


class Provider(_Strict):
    id: str
    name: str
    homepage: str | None = None
    documentation: str | None = None
    license: str | None = None
    commercial: bool = False
    authentication: Authentication = Authentication.NONE
    free: FreeTier
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


class RoutingDefaults(_Strict):
    strategy: RoutingStrategy
    fallback: FallbackMode
    selection: Selection


class RoutingPolicy(_Strict):
    strategy: RoutingStrategy | None = None
    fallback: FallbackMode | None = None
    selection: Selection | None = None


class RoutingConfig(_Strict):
    defaults: RoutingDefaults
    by_capability: dict[str, RoutingPolicy] = Field(default_factory=dict)


class Registry(_Strict):
    providers: list[Provider]
    families: list[Family] = Field(default_factory=list)
    routing: RoutingConfig


# --------------------------------------------------------------------------- #
# Loaders
# --------------------------------------------------------------------------- #

PROVIDERS_DIR = Path(__file__).resolve().parent.parent / "providers"
CAPABILITIES_FILE = "capabilities.yaml"
REGISTRY_FILE = "registry.yaml"


def _read_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path} did not parse to a mapping")
    return data


def load_catalogue(path: Path) -> Catalogue:
    return Catalogue.model_validate(_read_yaml(path))


def load_registry(path: Path) -> Registry:
    return Registry.model_validate(_read_yaml(path))


def manifest_present(providers_dir: Path = PROVIDERS_DIR) -> bool:
    return (providers_dir / CAPABILITIES_FILE).exists() and (providers_dir / REGISTRY_FILE).exists()
