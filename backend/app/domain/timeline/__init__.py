"""Timeline bounded context (Slice α6.3).

The **Timeline aggregate** is the *composition layer* of the platform: it places
registered media (α6.2) onto ordered **tracks** as time-ranged **clips**
(``Scene → Media → Clip → Timeline``). It is a **self-contained
optimistic-concurrency aggregate** (ADR-0038):

* The aggregate root is the ``Timeline`` (1:1 with a project — one live timeline
  per project). It alone carries a ``version`` column (``VersionMixin`` + the
  ``version_bump`` trigger); its children (``Track``, and — α6.3b — ``Clip``) do
  **not**. ``timelines.version`` is therefore the single OCC token for the whole
  aggregate: a track/clip mutation bumps it, and a fenced write against a stale
  token yields ``412``.
* It is **excluded** from the project-version ledger (ADR-0035): timeline edits
  do **not** bump ``projects.version`` and are **not** captured in
  ``project_versions`` snapshots / restore / diff. Ownership is derived through
  the project (the table carries no ``tenant_id`` / ``owner_user_id``).

This is a *third* concurrency posture, distinct from the editorial aggregate
(projects + scenes — aggregate OCC, in the ledger) and the generation artefacts
(prompts + media — last-writer-wins, no OCC). α6.3a ships the timeline root +
tracks; α6.3b adds clips.
"""
