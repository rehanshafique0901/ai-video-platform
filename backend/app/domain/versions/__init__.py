"""Project Versions bounded context (Slice α5d).

Holds the :class:`~app.domain.versions.project_version.ProjectVersion`
aggregate — an **immutable** content snapshot of a project plus its ordered
scenes, and the lightweight
:class:`~app.domain.versions.project_version.ProjectVersionSummary` read
model used by the list endpoint. See ``docs/domain/PROJECT_AGGREGATE.md`` §6
and ADR-0035 for the full model (snapshot boundary, monotonic numbering,
lineage chain, and the row-OCC ``version`` vs the content-snapshot ledger
distinction).
"""
