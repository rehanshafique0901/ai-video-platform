# Phase 3 Slice α7.1 — `RenderJob` Aggregate — Pre-flight

> Status: **DRAFT — AWAITING SIGN-OFF.** The orchestration-era architecture and
> its runtime decisions (D1–D9) were signed off in
> [`docs/architecture/CONTENT_GENERATION_PIPELINE.md`](../architecture/CONTENT_GENERATION_PIPELINE.md)
> (2026-07-16). This doc resolves the **RenderJob-specific** open questions (§4).
> Nothing is implemented yet.
>
> Mirrors the α5/α6 discipline: ground in the physical schema → lock decisions →
> sign-off → branch → implement → CI → merge → tag. Read-only planning artefact
> per `docs/engineering/RUNBOOK_WAVE.md` §1.
>
> **Predecessor.** α6.3b (`v0.4.14`, `main` @ `415f66a`) — Timeline aggregate
> clips; closed the composition tree.
>
> **This is the first orchestration slice.** Up to α6 we built persistent domain
> models. α7 begins the distributed-workflow layer *over* those models. α7.1 is
> deliberately the lowest-risk entry point (D8): a new aggregate on an existing
> table with **no external providers and no background worker**.
>
> **Baseline versioning.** `main` is at `0.4.14` (tag `v0.4.14-phase3-alpha6.3b`).
> First α7.1 commit bumps `backend/app/main.py` → `"0.4.15-phase3-alpha7.1-dev"`.
> **Zero migrations** (D7) — the `render_jobs` table + ENUM + indexes + CHECK
> already exist in baseline `0001` (`docs/database/schema.md` §17).

---

## Section 1 — Scope

### 1.1 One-line thesis

α7.1 introduces the **`RenderJob` aggregate**: an owner-scoped, OCC-guarded
record of a request to render a project's timeline. It owns its **own** render
lifecycle state machine (D9) and is coordinated purely through its own status +
domain events — it never mutates, and is never folded into, `projects.version`
or `timelines.version`. **No renderer runs yet**: jobs are created in `queued`
and can be `canceled`; the transitions that require a worker
(`running`/`succeeded`/`failed`) are wired in a later slice (α8.x).

### 1.2 What's in

1. **RenderJob create + read + cancel**:
   - `POST   /projects/{id}/render-jobs` → create (`status=queued`).
   - `GET    /projects/{id}/render-jobs` → list (owner-scoped, ordered).
   - `GET    /projects/{id}/render-jobs/{job_id}` → read one.
   - `POST   /projects/{id}/render-jobs/{job_id}/cancel?version=<n>` → `canceled`.
2. **Ownership** via `project_id → projects.owner_user_id` (RenderJob has no owner
   column — same reachability pattern the schema uses for tenancy).
3. **Timeline validation** — `timeline_id` must resolve to the project's live
   timeline (the α6.3 aggregate); otherwise `422`.
4. **OCC** via the existing `render_jobs.version` column (`VersionMixin`); cancel
   is a fenced compare-and-swap → `412` on stale, `404`-before-`412`.
5. **Idempotency** honouring the existing `UNIQUE(project_id, idempotency_key)`.
6. **Status machine** enforcement (§4.2 of the blueprint): only the legal α7.1
   transitions (`create→queued`, `{queued,running}→canceled`).
7. **Domain** `RenderJob` entity; **repository** `IRenderJobRepository` +
   `RenderJobRepository`; **use cases** (create/list/get/cancel); **DTOs**;
   **router** (new `render_jobs.py` module); DI wiring; unit + integration tests;
   docs (`API_CONTRACT.md`, `CHANGELOG.md`, `ROADMAP.md`, a new
   `docs/domain/RENDER_JOB_AGGREGATE.md`, ADR-0039).

### 1.3 What's out (deferred)

- **The render worker / FFmpeg** — the code that actually renders (α8.x). α7.1
  produces no `running`/`succeeded`/`failed` transition and never sets
  `output_media_asset_id`, `started_at`, `finished_at`, or `progress` beyond the
  `'0.00'` default.
- **Release/Draft render modes + ProjectVersion binding (D1)** — see §4 Q1
  (recommend defer to the worker slice; there is no column for it and D7 forbids a
  migration in α7).
- **Distributed-lock acquisition** (`render_job:<id>`) — nothing contends without
  a worker; the key convention is documented, live acquisition is α8.x (§4 Q5).
- **Full idempotency-key ledger** (`idempotency_keys` table, response-hash
  replay) — α7.5. α7.1 uses only the table-level `UNIQUE(project_id,
  idempotency_key)` (§4 Q4).
- **Outbox relay worker** — α7.3. Whether α7.1 *produces* outbox rows now is §4
  Q6.
- **`ExportJob`** — a separate later slice (child of `RenderJob`).
- **Zero migrations.**

---

## Section 2 — Grounded facts (the physical `render_jobs` table)

From `backend/app/infrastructure/db/models/jobs.py` (baseline `0001`,
`schema.md` §17). `RenderJob(UUIDPrimaryKeyMixin, TimestampMixin, VersionMixin, Base)`:

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | `gen_random_uuid()` |
| `project_id` | UUID FK → `projects.id` | `ON DELETE CASCADE` |
| `timeline_id` | UUID FK → `timelines.id` | **`ON DELETE RESTRICT`** — a timeline with render history can't be hard-deleted |
| `workflow_run_id` | UUID FK → `workflow_runs.id`, nullable | `ON DELETE SET NULL` — set when a workflow drives the render (later); **null in α7.1** |
| `pipeline` | Text NOT NULL | render pipeline id (e.g. `ffmpeg`) — **no server default** |
| `pipeline_version` | Text NOT NULL | adapter semver — **no server default** |
| `queue` | Text NOT NULL | CHECK `queue IN ('critical','high','normal','low','background')` (`queue_valid`) — **no server default** |
| `priority` | Integer NOT NULL, server_default `0` | secondary ordering within a queue |
| `status` | `render_status` ENUM NOT NULL | `queued, running, succeeded, failed, canceled` — **no server default** (app sets `queued`) |
| `started_at` / `finished_at` | timestamptz, nullable | worker-set (deferred) |
| `progress` | Text NOT NULL, server_default `'0.00'` | decimal-as-text 0.00–100.00 (D5 — stays text) |
| `error` | JSONB, nullable | `{code,message,trace_id,retries}` — worker-set (deferred) |
| `output_media_asset_id` | UUID FK → `media_assets.id`, nullable | `ON DELETE SET NULL` — worker-set (deferred) |
| `idempotency_key` | Text, nullable | client-supplied dedupe token |
| `version` | Integer NOT NULL | `VersionMixin` — OCC token |
| timestamps | | `TimestampMixin` (`created_at`/`updated_at`) |
| **no `deleted_at`** | | render jobs are operationally terminal, **not soft-deletable** (schema §17) — cancel is a *status*, not a delete |

Constraints / indexes:
- `uq_render_jobs_project_id_idempotency_key` — `UNIQUE(project_id, idempotency_key)`.
- `queue_valid` CHECK (mirrored by the DTO → `422` before the write).
- `ix_render_jobs_status_priority_created_at` (`status`, `priority`, `created_at`)
  — the future queue-dispatch index.
- `ix_render_jobs_project_id_status` (`project_id`, `status`) — the list/read index.

Key consequences:
- **No soft-delete.** Unlike tracks/clips, there is no `deleted_at`. "Removing" a
  job = `cancel` (a status transition). This changes the idempotent-by-404 shape:
  a canceled job still exists and is still `GET`-able.
- **`pipeline`/`pipeline_version`/`queue`/`status` have no server default** → the
  application layer must supply all four on insert. This is what makes Q6 (what
  `pipeline`/`pipeline_version` to store with no renderer) a real question.
- `version` is a real OCC column (unlike tracks/clips which borrowed the
  timeline's token) — so `RenderJob` fences on **its own** `version`, the α5/α6.2
  self-versioned pattern (`Project`, `MediaAsset`), not the α6.3 borrowed-token
  pattern.

---

## Section 3 — Decisions (recommended)

- **D3.1 — Self-owned aggregate + OCC (D9).** `RenderJob` is its own aggregate
  root with its own `version`. It owns the render state machine and is the sole
  writer of `render_jobs.status`. It never touches `projects.version` /
  `timelines.version`; cross-aggregate coordination is event-only (blueprint
  §7.1). Follows the α6.2 `MediaAsset` self-versioned shape.
- **D3.2 — Routing.** New router module `app/api/v1/routers/render_jobs.py`,
  prefix `/projects/{project_id}/render-jobs`. Nested under the project (the
  ownership anchor), *not* under the timeline — a render job is a project-level
  operation that references a timeline, and later carries a `workflow_run_id`.
- **D3.3 — Visibility gate / ownership.** Two-level uniform `404`: project (owned
  by caller) → render job (belongs to that project). Ownership is via
  `project.owner_user_id` (the α5a/α6.3 lineage); no owner column on the job.
- **D3.4 — Status machine (α7.1 subset).** `create → queued`;
  `cancel: {queued, running} → canceled`. `running/succeeded/failed` are **not**
  reachable via the API in α7.1 (no worker). Cancel from a terminal state
  (`succeeded`/`failed`/`canceled`) is a no-op-by-idempotency (see D3.6).
- **D3.5 — Cancel is a fenced status transition, not a delete.** `POST
  …/render-jobs/{id}/cancel?version=<n>`: `404`-before-`412`; `412` on stale
  `version`; success sets `status=canceled`, bumps `version`. A `DELETE` verb is
  **not** offered (no `deleted_at`; jobs are audit records).
- **D3.6 — Cancel idempotency.** Cancelling an already-`canceled` job returns
  `200` with the job unchanged (idempotent), **not** `409`/`412` — mirrors the
  idempotent-by-404 spirit adapted to a non-soft-deletable row. Cancelling a
  `succeeded`/`failed` job → `409 Conflict` (can't cancel completed work).
- **D3.7 — Ordering.** `GET …/render-jobs` returns `created_at DESC, id DESC`
  (newest first — a job feed), a total order (`id` tiebreak) for deterministic
  pagination. *(Note: differs from the α6 `start_seconds ASC` composition
  ordering because this is an activity feed, not a composition.)*
- **D3.8 — DTO validation → `422` before the write.** `queue ∈` the five values
  (mirror the CHECK); `priority >= 0` (app policy; DB allows any int but negative
  priority is meaningless); `timeline_id` present and a valid UUID. Bad values →
  `422`, never a `500` from the CHECK.
- **D3.9 — `RenderStatus` as a domain enum.** Introduce a framework-free
  `RenderStatus` enum in `app/domain/render/` (the five values) so the use cases
  guard transitions in the domain layer, not with string literals.
- **D3.10 — `RenderJob` owns only orchestration metadata (boundary invariant).**
  `RenderJob` owns *the request to render and its lifecycle status* — nothing
  else. It does **not** own rendered files, exported files, workflow state, or
  timeline edits; those belong to `MediaAsset`, `ExportJob`, `WorkflowRun`, and
  `Timeline` respectively. `RenderJob` only *references* them (`timeline_id`,
  `workflow_run_id`, `output_media_asset_id` are FKs, not embedded state) and
  *coordinates* via events (D9). This invariant prevents `RenderJob` from slowly
  accreting unrelated responsibilities.

  ```
  RenderJob  ── coordinates rendering (owns: queue, priority, status, error envelope)
  MediaAsset ── owns the produced rendered/exported asset
  WorkflowRun── owns orchestration/graph state
  Timeline   ── owns edit state
  ```

---

## Section 4 — Open questions for sign-off

**Q1 — Release/Draft render modes + ProjectVersion binding (D1 surface).**
D1 (accepted) says Release Renders bind to a frozen `ProjectVersion` and Draft
Renders use the live timeline. But: (a) `render_jobs` has **no `mode` or
`project_version_id` column**, (b) D7 forbids a migration in α7, and (c)
ADR-0038 explicitly **excludes the timeline from `project_versions` snapshots**,
so "bind the render to a ProjectVersion" needs a defined meaning for how the
*timeline* is frozen.
**Accepted with clarification (defer execution semantics, keep the conceptual
model).** α7.1 exposes a **single `RenderJob` aggregate only** — **no `mode`
field**, **no persisted `ProjectVersion` binding**. The blueprint (§9, D1) records
the architectural direction; α7.1's docs (`RENDER_JOB_AGGREGATE.md` / ADR-0039)
state explicitly that **the worker (α8.x) is responsible for resolving whether a
job executes against the live timeline (Draft) or a frozen `ProjectVersion`
(Release)**. When the worker lands, the binding mechanism (a scoped schema
decision — metadata/`project_version_id` column, or snapshotting the timeline
into the job) and its ADR-0038 reconciliation are decided *there*. This keeps
α7.1 completely migration-free while preserving the direction. *(Alternative:
add a column/mode now — rejected: breaks D7 and pre-commits the ADR-0038
reconciliation before the worker exists.)*

**Q2 — `pipeline` / `pipeline_version` values with no renderer (D3 §2).** Both
are `NOT NULL` with no server default, but no render pipeline exists in α7.1.
Options: (a) **server-assigned placeholder** — `pipeline='ffmpeg'`,
`pipeline_version='0.0.0'` (the intended default renderer, version 0 = "not yet
runnable"); (b) require the client to pass them; (c) accept them as optional in
the DTO with the (a) defaults.
**Recommend (c):** optional in the DTO, default to `pipeline='ffmpeg'` /
`pipeline_version='0.0.0'`. The client shouldn't need to know pipeline internals
in α7.1, and the placeholder version makes "this job predates a working renderer"
explicit and greppable. *(Alternative: 'unbound'/'0.0.0' to avoid implying ffmpeg
is wired.)*

**Q3 — Cancel semantics from terminal/queued states (confirm D3.4/D3.6).**
**Recommend confirm:** `queued`/`running` → `canceled`; re-cancel of `canceled`
→ `200` idempotent no-op; cancel of `succeeded`/`failed` → `409`. Flagging
because "cancel" on a non-soft-deletable job behaves differently from the α6
`DELETE` idempotent-by-404 pattern the codebase has used until now.

**Q4 — Idempotency behaviour on `(project_id, idempotency_key)` collision.**
The table has `UNIQUE(project_id, idempotency_key)` but the full
`idempotency_keys` ledger (response-hash replay) is α7.5.
**Recommend:** `idempotency_key` optional on create; on a repeat create with the
**same** key, return the **existing** job with `200` (lightweight true
idempotency) instead of `201`. A unique-violation race that slips past the
pre-check surfaces as `409`. *(Alternative: always `409` on duplicate key —
simpler but not idempotent; or wire the full ledger now — scope creep into α7.5.)*

**Q5 — Distributed-lock (`render_job:<id>`) in α7.1.** Nothing contends without a
worker.
**Recommend: document the key convention only; no live acquisition in α7.1.**
The `distributed_locks` repository/helper is built in the worker slice (α8.x)
where it is actually needed. *(Alternative: build a thin `ILockRepository`
skeleton now for a smaller α8.x — but YAGNI until the worker exists.)*

**Q6 — Outbox event production in α7.1 (D9).** The relay worker is α7.3, but the
*producer* side (writing rows to `event_outbox` in the same transaction) is
cheap and is exactly the D9 coordination mechanism.
**Recommend: produce outbox rows now** — `RenderJobCreated` and
`RenderJobCanceled` written to `event_outbox` in the same UoW transaction as the
state change. They simply accumulate (`published_at IS NULL`) until the α7.3
relay ships. This establishes the D9 pattern from the first orchestration slice
and makes α7.3 a pure consumer-side addition. *(Alternative: defer all event
production to α7.3 — but then α7.1's writes are silent and D9 is unexercised.)*

**Q7 — Version number bump.** α7.1 continues the running `0.4.x` slice cadence →
`0.4.15-phase3-alpha7.1-dev`. Flagging only because α7 marks the era shift
(domain → orchestration); if you'd prefer to mark that with a **minor bump to
`0.5.0-phase3-alpha7.1-dev`**, say so. **Recommend `0.4.15`** (monotonic slice
cadence; still Phase 3).

---

## Section 5 — Planned surface (pending §4)

```
POST   /api/v1/projects/{id}/render-jobs
  body:  { timeline_id, queue?='normal', priority?=0,
           pipeline?='ffmpeg', pipeline_version?='0.0.0', idempotency_key? }
  → 201  { data: RenderJobPublic }        (status=queued, version=1)
  → 200  { data: RenderJobPublic }        (idempotent replay, same idempotency_key — Q4)
  → 404 (project missing/not owned) · 422 (bad timeline_id / queue / priority) · 401

GET    /api/v1/projects/{id}/render-jobs
  → 200  { data: [RenderJobPublic … created_at DESC] } · 404 · 401

GET    /api/v1/projects/{id}/render-jobs/{job_id}
  → 200  { data: RenderJobPublic } · 404 · 401

POST   /api/v1/projects/{id}/render-jobs/{job_id}/cancel?version=<n>
  → 200  { data: RenderJobPublic }        (status=canceled, version bumped; idempotent if already canceled)
  → 404 (missing/not owned) · 409 (already succeeded/failed) · 412 (stale version) · 401
```

`RenderJobPublic` surfaces: `id`, `project_id`, `timeline_id`, `workflow_run_id`
(null in α7.1), `pipeline`, `pipeline_version`, `queue`, `priority`, `status`,
`progress`, `error` (null), `output_media_asset_id` (null), `started_at`/
`finished_at` (null), `idempotency_key`, `version`, `created_at`/`updated_at`.

Implementation order (mirrors α6.2/α6.3): domain `RenderJob` + `RenderStatus`
enum → `IRenderJobRepository` + `RenderJobRepository` + fakes/UoW wiring → render
use cases (create/list/get/cancel) + outbox producer (Q6) → DTOs + container
factories + deps aliases → new `render_jobs.py` router → unit tests → integration
tests → docs (`RENDER_JOB_AGGREGATE.md`, ADR-0039, `API_CONTRACT.md`,
`CHANGELOG.md`, `ROADMAP.md`) → CI gate → merge → tag
`v0.4.15-phase3-alpha7.1`.

---

## Section 6 — Reviewer sign-off

**SIGNED OFF (2026-07-16).** All seven §4 questions accepted:

- **Q1 — Release/Draft binding:** ✅ Accept with clarification. α7.1 exposes a
  single `RenderJob` aggregate — **no `mode` field, no persisted ProjectVersion
  binding**. Docs state the worker (α8.x) resolves Draft (live timeline) vs
  Release (frozen `ProjectVersion`). Migration-free; direction preserved.
- **Q2 — Pipeline defaults:** ✅ `pipeline="ffmpeg"`, `pipeline_version="0.0.0"`,
  optional in the DTO. Meaningful later when multiple renderers exist.
- **Q3 — Cancel semantics:** ✅ `queued→canceled`, `running→canceled` (best
  effort), `canceled→canceled` = `200` no-op, `succeeded`/`failed`→cancel =
  `409`.
- **Q4 — Idempotency:** ✅ Repeat `idempotency_key` returns the existing
  `RenderJob` (`200`). Full ledger deferred to the broader idempotency slice.
- **Q5 — Distributed lock:** ✅ Establish the `render_job:{id}` naming convention
  only; acquisition + lease renewal are α8.
- **Q6 — Outbox:** ✅ Strongly accepted. Produce `RenderJobCreated` /
  `RenderJobCanceled` outbox rows now (validates outbox persistence, event shape,
  aggregate ownership) — no dispatcher yet.
- **Q7 — Version:** ✅ `0.4.15-phase3-alpha7.1-dev` (stay on 0.4.x; still Phase-3
  architectural infrastructure, not a product milestone).

Plus **D3.10** (boundary invariant): `RenderJob` owns only orchestration
metadata — not rendered/exported files, workflow state, or timeline edits.

Proceed: branch `phase3/alpha7.1-renderjob`, bump `app/main.py` →
`0.4.15-phase3-alpha7.1-dev`, implement in the §5 order.
