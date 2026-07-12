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

from app.domain.projects.project import Project
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


@dataclass(frozen=True, slots=True)
class VersionDiffResult:
    """Coarse base→target change summary between two versions (α5d.2 §7).

    Computed on demand from two stored snapshots — never persisted (the
    α5d.1 ``diff_summary`` column stays ``null``). ``base`` is the ``against``
    version, ``target`` is the path version (Q8). ``project_changed`` is true
    when any project business column differs; the scene counts are keyed on
    scene ``id`` (added = target-only, removed = base-only, modified = in both
    with differing captured columns). Field-level detail is deferred (α5d.3+).
    """

    base_version_number: int
    target_version_number: int
    project_changed: bool
    scenes_added: int
    scenes_removed: int
    scenes_modified: int


@dataclass(frozen=True, slots=True)
class VersionBranchResult:
    """Outcome of a successful ``BranchProjectVersion.execute`` — α5d.3.

    ``project`` is the **new** independent project forked from the source
    version's snapshot (its ``version`` reflects the post-branch OCC token, so
    the router's ``ProjectPublic`` reports the correct handle). The ``source_*``
    fields carry the provenance breadcrumb the router echoes into the response
    ``meta.branched_from`` (Q7) — the same block persisted inside the new
    project's v1 snapshot. The two projects are independent after the fork;
    ``branched_from`` is a one-way historical link, not a live coupling.
    """

    project: Project
    source_project_id: UUID
    source_version_id: UUID
    source_version_number: int
