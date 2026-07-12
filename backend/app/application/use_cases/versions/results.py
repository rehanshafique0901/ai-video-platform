"""Result types for the α5d.1 Project Version use cases.

Each read/create use case pairs its version payload with the project's
``current_version_id`` (resolved from the ownership-gate ``Project``) so the
router can derive the ``is_current`` flag on the wire without a second query
(α5d pre-flight §4). Keeping ``current_version_id`` out of the domain
``ProjectVersion`` entity is deliberate: "which version is current" is a
property of the *project*, not of an immutable snapshot, so it travels
alongside — not inside — the version.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from app.domain.versions.project_version import ProjectVersion, ProjectVersionSummary


@dataclass(frozen=True, slots=True)
class VersionResult:
    """A single version plus the project's current-version pointer."""

    version: ProjectVersion
    current_version_id: UUID | None


@dataclass(frozen=True, slots=True)
class VersionListResult:
    """A version-history metadata list plus the project's current-version pointer."""

    versions: list[ProjectVersionSummary]
    current_version_id: UUID | None
