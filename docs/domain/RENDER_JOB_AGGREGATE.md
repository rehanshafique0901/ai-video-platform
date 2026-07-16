# RenderJob Aggregate

> **Convention.** This is a domain design document (companion to
> `docs/domain/PROJECT_AGGREGATE.md`, `docs/domain/MEDIA_AGGREGATE.md`, and
> `docs/domain/TIMELINE_AGGREGATE.md`). It defines the **RenderJob** aggregate —
> its identity, boundary, **derived** (project-scoped) ownership, and the defining
> stance: the **first orchestration aggregate**, a **self-versioned** record of a
> render request that owns its own lifecycle **status machine** and is coordinated
> purely through its status + **domain events on the `event_outbox`**. It is the
> design authority for Phase 3 **α7.1** (`RenderJob` CRUD + cancel — **no
> worker**). Read it alongside the α7.1 pre-flight
> (`docs/engineering/PHASE3_ALPHA7_1_PREFLIGHT.md`), the pipeline blueprint
> (`docs/architecture/CONTENT_GENERATION_PIPELINE.md`), and **ADR-0039**.
>
> **Grounding.** Every schema claim is checked against the live ORM
> (`backend/app/infrastructure/db/models/jobs.py`) and the baseline migration
> (`backend/alembic/versions/0001_baseline.py`), not an idealised model. Two
> baseline facts anchor the design: `render_jobs` carries `VersionMixin` (a real
> `version` + guarded bump trigger) but **no `SoftDeleteMixin`** (no `deleted_at`),
> and it carries **no** `tenant_id` / `owner_user_id` (ownership is derived through
> the project). So the job is self-versioned, not soft-deletable, and
> project-scoped.

---

## 1. Purpose & position in the model

A **RenderJob** is the **request to render a project's timeline** and the record
of that request's lifecycle. It is the first **orchestration** aggregate — contrast
the α5–α6 *domain-model* aggregates (Project, Scene, Prompt, Media, Timeline),
which hold persistent editorial/composition state. A RenderJob holds *runtime
orchestration* state: which queue, what priority, what status, and (later, via the
worker) progress and an error envelope.

Position in the hierarchy (baseline schema fact):

```
Project (α4/α5)
  └── Timeline (α6.3, 1:1)  ──referenced by──▶  RenderJob (render_jobs.timeline_id)   ← α7.1
        └── Track ── Clip

RenderJob ──(worker, α8.x)──▶ MediaAsset (output_media_asset_id)   ← the produced render
RenderJob ──(later)──────────▶ WorkflowRun (workflow_run_id)        ← when a workflow drives it
```

The critical separation this document establishes (ADR-0039, pre-flight D3.10):

> **A RenderJob owns only orchestration metadata — the request to render and its
> lifecycle status. It does not own rendered files, exported files, workflow
> state, or timeline edits; it merely references them by FK and coordinates via
> events.**

This is a **self-versioned OCC** posture (like `Project` / `MediaAsset`), *not*
the borrowed-token posture of the timeline (ADR-0038): `render_jobs.version` is a
real column and fences the job on **its own** token.

---

## 2. Aggregate boundary

### 2.1 The aggregate root — `RenderJob`

The domain `RenderJob` is a slim, frozen view of the physical `render_jobs` row:

- `id` — durable UUID, server-minted.
- `project_id` — the ownership anchor (FK → `projects.id`, `ON DELETE CASCADE`).
  Ownership is **derived** through the project — the table carries **no**
  `tenant_id` / `owner_user_id`.
- `timeline_id` — the timeline to render (FK → `timelines.id`, `ON DELETE
  RESTRICT` — a timeline with render history can't be hard-deleted). Resolved
  **server-side** (1:1 with the project); the client renders "the project", not a
  chosen timeline.
- `workflow_run_id` — optional link set when a workflow drives the render (FK →
  `workflow_runs.id`, `ON DELETE SET NULL`). **Null in α7.1.**
- `pipeline` / `pipeline_version` — the renderer identity, `NOT NULL`, no server
  default. Default to `'ffmpeg'` / `'0.0.0'` in the DTO (the placeholder version =
  "predates a working renderer"; meaningful once multiple renderers exist). **Set
  on create, immutable thereafter in α7.1.**
- `queue` — one of `critical, high, normal, low, background` (CHECK
  `queue_valid`), no server default. Scheduling hint. **Set on create.**
- `priority` — secondary ordering within a queue, server-default `0` (DTO clamps
  `0–1000`). **Set on create.**
- `status` — the `render_status` ENUM (`queued, running, succeeded, failed,
  canceled`), no server default (the app sets `queued`). **The lifecycle token
  (§4).**
- `started_at` / `finished_at` — worker-set timestamps. **Null in α7.1.**
- `progress` — decimal-as-text `'0.00'`–`'100.00'`, server-default `'0.00'`.
  Progress semantics are application-layer (blueprint D5 — stays text). **`'0.00'`
  in α7.1.**
- `error` — JSONB `{code, message, trace_id, retries}`, worker-set. **Null in
  α7.1.**
- `output_media_asset_id` — the produced render (FK → `media_assets.id`, `ON
  DELETE SET NULL`), worker-set. **Null in α7.1.**
- `idempotency_key` — optional client dedupe token; unique per project
  (`uq_render_jobs_project_id_idempotency_key`).
- `version` — **the self-OCC token** (§4). Server-owned.
- `created_at` / `updated_at` — timestamps; `updated_at` is trigger-owned.

### 2.2 The defining facts: self-versioned, not soft-deletable

`RenderJob` is `UUIDPrimaryKeyMixin + TimestampMixin + VersionMixin` and **is** in
`_VERSION_BUMP_TABLES` (guarded `tg_render_jobs_biu_version_bump`) — a real
`version`, fenced on its own token (contrast `Track`/`Clip`, which borrow the
timeline's). It carries **no `SoftDeleteMixin`**: there is no `deleted_at`. A
render job is an operationally terminal **audit record** — "removing" it is a
`cancel` *status transition*, and a canceled job stays `GET`-able.

---

## 3. Ownership, scoping & anti-enumeration

Ownership is **derived through the project** (the `render_jobs` table has no owner
columns). Every endpoint is authenticated (`CurrentUserDep`) and runs a
**two-level gate**:

- **Project gate** — `IProjectRepository.get_owned(project_id, tenant, owner)`;
  `None` → uniform `404 NOT_FOUND` (missing / soft-deleted / not the caller's).
- **Render-job gate** — `RenderJobRepository.get_owned(project_id, render_job_id)`;
  `None` → uniform `404` (unknown, or under another user's project —
  anti-enumeration, pre-flight D3.3).

On **create**, a third check resolves the project's timeline
(`TimelineRepository.get_by_project`); a project with no timeline is a **`422`**
(the request is well-formed but not fulfillable — the project IS visible, so it is
not a `404`).

`idempotency_key` uniqueness is enforced by the partial-unique index; a repeat key
returns the **existing** job (`200`, §6), and a unique-violation race surfaces as
`ConflictError` resolved by returning the winner.

---

## 4. Concurrency & lifecycle: a self-owned status machine (ADR-0039)

`render_jobs.version` is the job's **own** OCC token, and `status` is its
lifecycle. The α7.1 state machine subset (no worker → no
`running`/`succeeded`/`failed` via the API):

```
             create
        ∅ ───────────▶ queued
                          │
              cancel      │  cancel
        ┌─────────────────┼──────────────────┐
        ▼                 ▼                   ▼
     canceled ◀──── running (worker, α8.x)   (succeeded / failed — worker, α8.x)
     (200 no-op                               cancel ⇒ 409
      on re-cancel)
```

- **Create** → `status = queued`, `version = 1`, `progress = '0.00'`. Emits
  `RenderJobCreated` (§5).
- **Cancel** (`POST …/cancel`, `version` in the body) is a **version-fenced CAS**:
  the repository sets `status = canceled` + `version + 1` (net +1 over the guarded
  trigger), predicated on the observed `version` AND `status IN
  ('queued','running')`. Control flow is **404-before-classify-before-412**:
  - project/job not visible → `404`;
  - already `canceled` → **`200` idempotent no-op** (no event, no version bump);
  - `succeeded`/`failed` → **`409`** (completed work is not cancelable);
  - cancelable but stale `version` → **`412`**;
  - success → `200` with `status = canceled`, `version` bumped. Emits
    `RenderJobCanceled` (§5).

The `status IN ('queued','running')` predicate makes the terminal guard
**race-safe at the DB**: a worker completing the job between the read and the CAS
write yields zero rows → `None` → the use case re-classifies (canceled → `200`;
terminal → `409`; else stale → `412`). A `RenderJob` mutation **never** bumps
`projects.version` or `timelines.version`.

A `DELETE` verb is **not** offered (no `deleted_at`; jobs are audit records) — this
is why re-cancel is a `200` no-op, not the α6 idempotent-by-404 shape.

---

## 5. Cross-aggregate coordination: the transactional outbox (D9)

A RenderJob coordinates with the rest of the system **only** through domain events
written to the `event_outbox` in the **same UnitOfWork transaction** as the state
change (blueprint §6 / D9 — the transactional-outbox guarantee):

- `RenderJobCreated` — on create.
- `RenderJobCanceled` — on a real cancel (not on an idempotent no-op).

Event bodies carry **orchestration fields only** (`render_job_id`, `project_id`,
`timeline_id`, `pipeline`, `pipeline_version`, `queue`, `priority`, `status`,
`version`); a consumer needing rendered-file or timeline state resolves it from
the referenced aggregates (§1 boundary). `event_version` starts at `"1.0"`.

α7.1 only **produces** rows — they accumulate (`published_at IS NULL`) until the
α7.3 relay ships. There is **no dispatcher** in α7.1; no aggregate directly
mutates another's state. This exercises the D9 pattern from the first
orchestration slice and makes α7.3 a pure consumer-side addition.

---

## 6. Idempotency

`idempotency_key` is **optional** on create (pre-flight Q4). A repeat create with
the **same** key for the project returns the **existing** job with `200`
(lightweight true idempotency), not a duplicate `201`. The table-level
`uq_render_jobs_project_id_idempotency_key` unique is the race-safe backstop
behind the use case's pre-check: a unique-violation that slips past the pre-check
is resolved by returning the winning job. The full `idempotency_keys` ledger
(response-hash replay, ADR-0031) is deferred to α7.5.

---

## 7. Structured-log posture

RenderJob lifecycle events are logged with identifiers + field names only:

- `render_job.created` (INFO) — `render_job_id`, `project_id`, `timeline_id`,
  `pipeline`, `pipeline_version`, `queue`, `priority`, `idempotency_key`,
  `owner_user_id`, `ip`.
- `render_job.create_idempotent_replay` / `render_job.create_idempotent_race`
  (INFO) — `render_job_id`, `project_id`, `idempotency_key`, `owner_user_id`, `ip`.
- `render_job.canceled` (INFO) — `render_job_id`, `project_id`, `previous_version`,
  `new_version`, `owner_user_id`, `ip`.
- `render_job.cancel_noop` (INFO, idempotent re-cancel) /
  `render_job.cancel_rejected` (WARNING, `reason` ∈ `terminal_state` /
  `version_mismatch` / `version_mismatch_cas`) — `render_job_id`, `project_id`,
  `status` / `expected_version`, `owner_user_id`, `ip`.

---

## 8. Open evolution (explicitly out of α7.1)

- **The render worker (α8.x).** Drives `queued → running → {succeeded, failed}`,
  sets `output_media_asset_id` / `started_at` / `finished_at` / `progress` /
  `error`, and acquires the `render_job:{id}` distributed lock (ADR-0032). α7.1
  never writes any of these.
- **Release/Draft binding (D9 / pre-flight Q1).** Whether a job renders against
  the live timeline (Draft) or a frozen `ProjectVersion` (Release) is the
  **worker's** decision (α8.x) — α7.1 persists no `mode` / `project_version_id`
  and adds no migration.
- **Outbox relay (α7.3).** Publishes the events this slice produces; stamps
  `published_at`.
- **Idempotency-key ledger (α7.5).** Response-hash replay over `idempotency_keys`.
- **`ExportJob`.** A child orchestration aggregate downstream of `RenderJob`,
  reusing this posture.

---

## 9. Change log

| Date | Change |
|---|---|
| 2026-07-16 | Initial authoring for Phase 3 α7.1 (`RenderJob` aggregate — the first orchestration slice). Establishes the self-versioned OCC posture (`render_jobs.version`; contrast the timeline's borrowed token), derived (project-scoped) ownership + two-level visibility gate, server-side timeline resolution (`422` when absent), the orchestration-only boundary invariant (D3.10), the `queued`/`canceled` status-machine subset (cancel is a version-fenced CAS with a race-safe terminal guard; re-cancel `200` no-op; terminal `409`; stale `412`; no `DELETE`), idempotent create (repeat key → existing job `200`), and the transactional-outbox coordination (`RenderJobCreated` / `RenderJobCanceled` produced now, relay α7.3). Worker-owned fields (`output_media_asset_id`, `started_at`, `finished_at`, `error`, `progress` beyond `'0.00'`) and Release/Draft binding are deferred to α8.x. Adopts ADR-0039. |
