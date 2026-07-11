"""Shared result type for the single-scene use cases (Slice α5c).

Create / Get / Update / Move all return one persisted scene plus its dense
1-based display ``position`` (computed from the sparse ``scene_number`` —
the raw key is never exposed, α5c Q6). The router projects both into
``ScenePublic``. The success/no-op or reorder/rebalance distinctions are
recorded in each use case's structured log, not carried on the wire, so a
single flat result type suffices (mirrors the α5b ``UpdateProjectResult``
pattern where ``changed`` is log-only and the router ignores it).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.scenes.scene import Scene


@dataclass(frozen=True, slots=True)
class SceneResult:
    """A persisted scene plus its dense 1-based display position."""

    scene: Scene
    position: int
