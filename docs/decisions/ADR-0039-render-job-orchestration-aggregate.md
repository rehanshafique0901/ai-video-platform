# ADR-0039 — The `RenderJob` Is a Self-Owned Orchestration Aggregate (coordinated by status + outbox events)

**Status:** Proposed (documents the pattern shipped in Phase 3 α7.1 — the first
orchestration slice). Flips to Accepted on merge of this ADR PR.
**Opens the orchestration era.** Up to α6 every aggregate was a *persistent
domain model* (Project, Scene, Prompt, Media, Timeline). `RenderJob` is the first
*orchestration* aggregate: it owns a **runtime lifecycle state machine** and is
coordinated **purely through its own status + domain events on the
`event_outbox`** — no aggregate directly mutates another's state.
**Reuses, does not invent, a concurrency posture.** `RenderJob` adopts the
**self-versioned OCC** posture of ADR-0037's siblings-with-a-`version`
(`render_jobs.version` is a real column, like `projects.version` /
`media_assets` — *not* the borrowed-token model of ADR-0038's timeline). It does
**not** mutate ADR-0035 (project-version snapshots), ADR-0036 (prompts),
ADR-0037 (media), or ADR-0038 (timeline).
**Refines / documents:** `docs/domain/RENDER_JOB_AGGREGATE.md`,
`docs/architecture/CONTENT_GENERATION_PIPELINE.md` (§7.1 event coordination / D9
outbox), `API_CONTRACT.md` §3.2.5 (new Render Jobs resource), and the α7.1
pre-flight (`docs/engineering/PHASE3_ALPHA7_1_PREFLIGHT.md`, Q1–Q7 + D3.1–D3.10).
Builds on **ADR-0031** (idempotency-keys), **ADR-0032** (distributed-locks
lease), **ADR-0034** (authenticated endpoint pattern), **ADR-0037** (media
self-versioned OCC).
**Wave:** Phase 3, orchestration slice α7.1 (`RenderJob` aggregate root — CRUD +
cancel; **no worker**). The background render worker (and the
`running`/`succeeded`/`failed` transitions) is α8.x.

---

## Context

α7 begins the **content-generation / orchestration layer** *over* the persistent
domain models of α5–α6. α7.1 is deliberately the lowest-risk entry point: a new
aggregate on an **existing baseline table** with **no external providers and no
background worker**. The slice ships **zero migrations** (the `render_jobs` table,
the `render_status` ENUM, the `queue_valid` CHECK, the
`uq_render_jobs_project_id_idempotency_key` unique, and the dispatch/list indexes
all exist in baseline `0001`, `docs/database/schema.md` §17).

The physical schema signals the posture (α7.1 pre-flight §2):

- `render_jobs` is `RenderJob(UUIDPrimaryKeyMixin, TimestampMixin, VersionMixin,
  Base)` — it carries a **real `version` column** (unlike `tracks`/`clips`, which
  borrow `timelines.version`). Ownership is **derived through the project**
  (`project_id → projects.owner_user_id`); the table has **no** `tenant_id` /
  `owner_user_id`.
- There is **no `deleted_at`** — a render job is an operationally terminal audit
  record, **not soft-deletable**. "Removing" a job is a `cancel` *status
  transition*, not a delete.
- `pipeline`, `pipeline_version`, `queue`, and `status` are `NOT NULL` with **no
  server default** — the application layer must supply all four on insert.
- `workflow_run_id`, `output_media_asset_id`, `started_at`, `finished_at`, and
  `error` are worker-owned and stay at their queued defaults in α7.1.

Without an ADR, a future contributor sees a versioned table nested under a project
that references a timeline it does not own, with worker-only columns left null,
and cannot tell whether that is a **decision** or an **unfinished** shape to be
"completed" (e.g. by folding render state into the timeline, giving `RenderJob`
its own owner columns, or letting the create endpoint mutate the timeline). This
ADR promotes the implemented convention to a recorded decision.

---

## Decision

### D1 — `RenderJob` is a self-owned orchestration aggregate (pre-flight D3.1/D3.9)

The **aggregate root** is the `RenderJob`. It alone owns the render lifecycle and
is the **sole writer** of `render_jobs.status`. It owns a framework-free domain
enum `RenderStatus` (`queued, running, succeeded, failed, canceled`) whose
`is_terminal` / `is_cancelable` properties gate transitions in the domain layer
(not with string literals). The governing principle:

> **A `RenderJob` is the request to render a project's timeline and the record of
> that request's lifecycle. It owns its status machine and is coordinated purely
> through its own status + domain events — it never mutates, and is never folded
> into, `projects.version` or `timelines.version`.**

### D2 — Self-versioned OCC on `render_jobs.version` (pre-flight D3.1/D3.5)

`RenderJob` fences on **its own** `version` (the α5a/α6.2 self-versioned pattern of
`Project` / `MediaAsset`), *not* the α6.3 borrowed-token pattern. `cancel` is a
**version-fenced compare-and-swap**: the repository hand-sets `version + 1` over
the guarded `tg_render_jobs_biu_version_bump` trigger (net **+1**), predicated on
both the observed `version` AND `status IN ('queued','running')`. The
terminal-state guard is therefore **race-safe at the DB**: a worker that completes
the job between the use case's read and the CAS write cannot be silently
overwritten (RETURNING yields no row → `None` → the use case re-classifies).

### D3 — Owns only orchestration metadata (boundary invariant, pre-flight D3.10)

`RenderJob` owns *the request to render and its lifecycle status* — nothing else.
It does **not** own rendered files, exported files, workflow state, or timeline
edits; those belong to `MediaAsset`, `ExportJob`, `WorkflowRun`, and `Timeline`
respectively. `RenderJob` only *references* them (`timeline_id`,
`workflow_run_id`, `output_media_asset_id` are FKs, not embedded state) and
*coordinates* via events (D5). This prevents the aggregate from slowly accreting
unrelated responsibilities.

```
RenderJob  ── coordinates rendering (owns: queue, priority, status, error envelope)
MediaAsset ── owns the produced rendered/exported asset
WorkflowRun── owns orchestration/graph state
Timeline   ── owns edit state
```

### D4 — Cross-aggregate coordination is event-only; the outbox is exercised now (pre-flight Q6)

On every state change, `RenderJob` writes a domain event to the `event_outbox` in
the **same UnitOfWork transaction** as the mutation (the transactional-outbox
guarantee): `RenderJobCreated` on create, `RenderJobCanceled` on a real cancel.
α7.1 only **produces** rows (they accumulate with `published_at IS NULL`); the
relay that publishes them is α7.3, and there is **no dispatcher** in α7.1. This
establishes the D9 coordination pattern from the first orchestration slice and
makes α7.3 a pure consumer-side addition. Event bodies carry **orchestration
fields only** (identity + queue/priority/status/version) — a consumer that needs
rendered-file or timeline state resolves it from the referenced aggregates (D3).

### D5 — Project-nested routing; ownership derived through the project (pre-flight D3.2/D3.3)

Ownership is derived through the project, so the surface is **project-nested** and
every access runs a **two-level uniform-`404` gate** (project owned by caller →
render job belongs to that project). A render job is a project-level operation
that references a timeline (and later carries a `workflow_run_id`), so it is
nested under the project, **not** under the timeline.

```
POST   /api/v1/projects/{project_id}/render-jobs
GET    /api/v1/projects/{project_id}/render-jobs?status=<render_status>
GET    /api/v1/projects/{project_id}/render-jobs/{render_job_id}
POST   /api/v1/projects/{project_id}/render-jobs/{render_job_id}/cancel
```

The `timeline_id` is **resolved server-side** (1:1 with the project) — the client
renders "the project", not a chosen timeline. A project with no timeline is a
**`422`** (well-formed request, not fulfillable yet), not a `404` (the project IS
visible).

### D6 — Cancel is a fenced status transition, not a delete (pre-flight D3.4/D3.5/D3.6)

The α7.1 state machine subset:

```
create ─▶ queued
queued  ─▶ canceled   ✅
running ─▶ canceled   ✅ (best-effort; the worker observes the flag in α8.x)
canceled ─▶ canceled  ⇒ 200 idempotent no-op (no event, no version bump)
succeeded/failed ─▶ cancel ⇒ 409 (completed work is not cancelable)
```

`running`/`succeeded`/`failed` are **not** reachable via the API in α7.1 (no
worker). `cancel` carries the aggregate `version` in the **body** (a `POST`, since
it changes state). A `DELETE` verb is **not** offered (there is no `deleted_at`;
jobs are audit records). Because a canceled job still exists and is `GET`-able,
the idempotent shape differs from the α6 idempotent-by-404 delete: a re-cancel is
a **`200` no-op**, not a `404`.

### D7 — Idempotency: repeat key returns the existing job (pre-flight Q4)

`idempotency_key` is **optional** on create. A repeat create with the **same** key
for the project returns the **existing** job with `200` (lightweight true
idempotency) instead of `201`. The table-level
`uq_render_jobs_project_id_idempotency_key` unique is the race-safe backstop
behind the use case's pre-check: a unique-violation that slips past the pre-check
is resolved by returning the winner. The full `idempotency_keys` ledger
(response-hash replay) is deferred to α7.5.

### D8 — Pipeline / queue defaults; DTO validation → `422` before the write (pre-flight Q2/D3.8)

`pipeline` / `pipeline_version` are **optional** in the DTO, defaulting to
`pipeline='ffmpeg'` / `pipeline_version='0.0.0'` — the placeholder version makes
"this job predates a working renderer" explicit and greppable; the fields become
meaningful once multiple renderers exist (α8.x). `queue` (∈ the five values,
mirroring the `queue_valid` CHECK) and `priority` (`0–1000`, app policy) are
validated in the DTO so a bad value is a **`422`**, never a `500` from the CHECK.
Listing accepts an optional `?status=` filter validated against `render_status`
(bad enum → `422`).

### D9 — Release/Draft binding is deferred to the worker (pre-flight Q1)

The blueprint's D1 records that Release Renders bind to a frozen `ProjectVersion`
and Draft Renders use the live timeline — but `render_jobs` has **no `mode` /
`project_version_id` column**, D7 (zero migrations) forbids adding one in α7, and
ADR-0038 excludes the timeline from `project_versions` snapshots. So α7.1 exposes
a **single `RenderJob` aggregate only** — **no `mode` field, no persisted
`ProjectVersion` binding**. **The worker (α8.x) is responsible for resolving
whether a job executes against the live timeline (Draft) or a frozen
`ProjectVersion` (Release)**; the binding mechanism and its ADR-0038
reconciliation are decided *there*. This keeps α7.1 migration-free while
preserving the direction.

### D10 — Distributed-lock naming convention only (pre-flight Q5)

The lock key for a render job is `render_job:{id}`. α7.1 documents the convention
only; nothing contends without a worker, so live acquisition + lease renewal
(over `distributed_locks`, ADR-0032) are built in α8.x where they are needed.

---

## Alternatives Considered

1. **Fold render state into the Timeline (fence on `timelines.version`).**
   *Rejected.* A render is an *orchestration* concern with its own lifecycle,
   retries, queue, and error envelope — coupling it to the composition token would
   make every render state change a timeline edit and vice versa. The baseline
   deliberately gave `render_jobs` its own `version`.

2. **Give `RenderJob` its own `tenant_id` / `owner_user_id` columns.**
   *Rejected.* Ownership is reachable via `project_id → projects.owner_user_id`
   (the α5a/α6.3 lineage); duplicating owner columns invites drift. The two-level
   project gate is the established pattern.

3. **Last-writer-wins, no OCC (treat render jobs like media/prompts).**
   *Rejected.* Cancel races a worker's completion; without a version + terminal
   guard, a cancel could silently clobber a `succeeded` result. The baseline gave
   `render_jobs` a `version` the generation artefacts lack — this ADR uses it.

4. **A `DELETE` verb for cancel (idempotent-by-404).** *Rejected* (D6): there is
   no `deleted_at`; a canceled job is a retained audit record that stays
   `GET`-able. Cancel is a status transition, so re-cancel is a `200` no-op.

5. **Always `409` on a duplicate `idempotency_key`.** *Rejected* (D7): simpler but
   not idempotent. Returning the existing job (`200`) is lightweight true
   idempotency and defers the full ledger to α7.5 cleanly.

6. **Add a `mode` / `project_version_id` column now for Release/Draft.**
   *Rejected* (D9): breaks D7 (zero migrations) and pre-commits the ADR-0038
   reconciliation before the worker — the actual consumer of the binding — exists.

7. **Defer all outbox production to α7.3.** *Rejected* (D4): then α7.1's writes are
   silent and the D9 coordination mechanism is unexercised. Producing rows now is
   cheap and validates outbox persistence + event shape + aggregate ownership.

---

## Consequences

- **Positive — the orchestration era has a clear first pattern.** A self-owned
  aggregate + status machine + transactional outbox, with zero cross-aggregate
  mutation. Later orchestration aggregates (`ExportJob`, `WorkflowRun`) copy this
  shape.
- **Contract — cancel carries `version` in the body; there is no `DELETE`.** A
  stale token on a still-cancelable job is `412`; a terminal job is `409`; a
  re-cancel of a canceled job is a `200` no-op. Missing/foreign project or job →
  uniform `404`.
- **Correctness — the terminal guard is race-safe at the DB.** The CAS predicate
  `status IN ('queued','running')` means a worker completing the job mid-cancel is
  never overwritten; the use case re-classifies a lost CAS.
- **Positive — small, migration-free slice.** α7.1 is the aggregate + ownership
  gate + OCC cancel + idempotency + outbox producer — no worker, no providers, no
  new concurrency machinery.
- **Deferred — worker-owned fields stay null.** `workflow_run_id`,
  `output_media_asset_id`, `started_at`, `finished_at`, `error`, and any
  `progress` beyond `'0.00'` are set by the α8.x worker; α7.1 never writes them.

---

## Pattern Reference (Examples)

- **Domain:** `app/domain/render/render_job.py` (frozen `RenderJob`, **with**
  `version`), `app/domain/render/render_status.py` (`RenderStatus` enum —
  `is_terminal` / `is_cancelable`).
- **Repository:** `app/infrastructure/repositories/render_job_repository.py`
  (`RenderJobRepository`: `add` [idempotency unique → `ConflictError`],
  `get_by_project_and_key`, `list_by_project` [newest-first + `status` filter],
  `get_owned`, `cancel` [version-fenced CAS + race-safe terminal guard, net +1]);
  `app/infrastructure/repositories/event_outbox_repository.py`
  (`EventOutboxRepository`: append-only `add`).
- **Use cases:** `app/application/use_cases/render/*` — `CreateRenderJob`
  (idempotent, emits `RenderJobCreated`), `ListRenderJobs`, `GetRenderJob`,
  `CancelRenderJob` (fenced transition, emits `RenderJobCanceled`), `_events.py`
  (outbox event shape). None mutate another aggregate.
- **DTOs / router:** `app/api/v1/schemas/render.py`,
  `app/api/v1/routers/render_jobs.py` (project-nested; 201/200 create split;
  version in the cancel body).

New orchestration aggregates copy these shapes rather than reinventing them.

---

## Future Extensions

- **α7.3 — Outbox relay worker** — publishes the `RenderJobCreated` /
  `RenderJobCanceled` rows this slice produces and stamps `published_at`. Pure
  consumer-side addition.
- **α7.5 — Idempotency-key ledger** — the full `idempotency_keys` table +
  response-hash replay (ADR-0031); α7.1 uses only the table-level unique.
- **α8.x — The render worker** — drives `queued → running → {succeeded, failed}`,
  sets `output_media_asset_id` / `started_at` / `finished_at` / `progress` /
  `error`, acquires the `render_job:{id}` lock (ADR-0032), and **resolves
  Release/Draft binding** (D9).
- **Later — `ExportJob`** — a child orchestration aggregate downstream of
  `RenderJob`, reusing this posture.
